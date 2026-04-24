"""
scrapers/hotel_scraper.py
Scrapes Google Hotels via Playwright for each hotel in your curated list.
Runs against all trip date combos. Saves to Supabase price_snapshots.

Usage:
    python scrapers/hotel_scraper.py
    python scrapers/hotel_scraper.py --trip-id <uuid>   # single trip
    python scrapers/hotel_scraper.py --dry-run          # print without saving
"""

import os
import sys
import json
import time
import random
import asyncio
import logging
import argparse
import uuid as uuid_lib
from datetime import date, timedelta, datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import (
    get_active_hotels, get_active_trips, get_trip_hotels,
    save_hotel_snapshot, upsert_price_low
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

HEADLESS       = True
SLOW_MO        = 0
REQUEST_DELAY  = (3, 7)   # Random sleep range between hotel searches (seconds)
PRICE_TIMEOUT  = 20_000   # ms to wait for price elements
MAX_RETRIES    = 2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.94 Safari/537.36",
]


# ── Date range expander ─────────────────────────────────────────────────────────

def expand_date_combos(check_in_start: date, check_in_end: date,
                       durations: list[int]) -> list[tuple[date, date]]:
    """Generate all (check_in, check_out) combos for a trip config."""
    combos = []
    current = check_in_start
    while current <= check_in_end:
        for dur in durations:
            check_out = current + timedelta(days=dur)
            combos.append((current, check_out))
        current += timedelta(days=1)
    return combos


# ── Google Hotels scraper ───────────────────────────────────────────────────────

def build_google_hotels_url(search_query: str, check_in: date,
                             check_out: date, adults: int = 2) -> str:
    """
    Builds a Google Hotels search URL.
    e.g. https://www.google.com/travel/hotels/s/The%20Langham%20Chicago?checkin=2026-08-01&checkout=2026-08-04&adults=2
    """
    from urllib.parse import quote_plus
    q = quote_plus(search_query)
    ci = check_in.strftime("%Y-%m-%d")
    co = check_out.strftime("%Y-%m-%d")
    return (
        f"https://www.google.com/travel/hotels/s/{q}"
        f"?q={q}&checkin={ci}&checkout={co}&adults={adults}&hl=en&gl=us&curr=USD"
    )


async def scrape_google_hotels(page, hotel: dict, check_in: date,
                                check_out: date, adults: int = 2) -> list[dict]:
    """
    Navigate to Google Hotels for a specific hotel + dates,
    extract prices per provider from the hotel detail page.
    Returns list of {provider, price, room_type, cancellable}
    """
    url = build_google_hotels_url(hotel["search_query"], check_in, check_out, adults)
    results = []

    for attempt in range(MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            # ── Dismiss consent / cookie dialog if present ──────────────────
            try:
                consent = page.locator('button:has-text("Accept all"), button:has-text("I agree")')
                if await consent.count() > 0:
                    await consent.first.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # ── Try to click into the specific hotel listing ────────────────
            # Google Hotels shows a list; click the first result matching our hotel
            hotel_card = page.locator('[data-hveid]').first
            if await hotel_card.count() > 0:
                try:
                    await hotel_card.click()
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass

            # ── Extract prices from the "Prices" / "All options" panel ──────
            # Selector targets the price rows in the booking options panel
            price_rows = page.locator('[data-price], [jsname="priceRow"], .kCsRcb, [data-provider-name]')
            count = await price_rows.count()

            if count == 0:
                # Fallback: try featured price at top of page
                featured = page.locator('[data-hveid] [aria-label*="$"], [data-hveid] [data-price]').first
                if await featured.count() > 0:
                    price_text = await featured.inner_text()
                    price = _parse_price(price_text)
                    if price:
                        results.append({
                            "provider": "Google Hotels",
                            "price": price,
                            "room_type": None,
                            "cancellable": None,
                        })

            for i in range(min(count, 10)):  # cap at 10 providers per hotel
                try:
                    row = price_rows.nth(i)
                    row_text = await row.inner_text()

                    # Extract provider name
                    provider = "Unknown"
                    try:
                        prov_el = row.locator('[data-provider-name], .vQlnEc, .GZnc6d')
                        if await prov_el.count() > 0:
                            provider = (await prov_el.first.inner_text()).strip()
                    except Exception:
                        pass

                    # Extract price
                    price = _parse_price(row_text)
                    if not price:
                        continue

                    # Extract cancellable hint
                    cancellable = None
                    try:
                        cancel_el = row.locator('[aria-label*="cancel"], [aria-label*="refund"]')
                        if await cancel_el.count() > 0:
                            label = await cancel_el.first.get_attribute("aria-label") or ""
                            cancellable = "free cancel" in label.lower()
                    except Exception:
                        pass

                    results.append({
                        "provider": provider or "Google Hotels",
                        "price": price,
                        "room_type": None,
                        "cancellable": cancellable,
                    })
                except Exception as e:
                    logger.debug(f"Row parse error: {e}")

            # If we got at least one result, we're done
            if results:
                logger.info(
                    f"  ✓ {hotel['name']} | {check_in} → {check_out} | "
                    f"{len(results)} provider(s) found"
                )
                break

            # No results — maybe we landed on search results list
            # Try extracting the lowest price from the list view
            list_prices = page.locator('[data-price], .kCsRcb span[aria-label*="$"]')
            list_count = await list_prices.count()
            if list_count > 0:
                for j in range(min(list_count, 3)):
                    try:
                        pt = await list_prices.nth(j).inner_text()
                        price = _parse_price(pt)
                        if price:
                            results.append({
                                "provider": "Google Hotels",
                                "price": price,
                                "room_type": None,
                                "cancellable": None,
                            })
                    except Exception:
                        pass
                if results:
                    break

            logger.warning(f"  ⚠ No prices found for {hotel['name']} ({check_in}), attempt {attempt+1}")

        except PWTimeout:
            logger.warning(f"  Timeout for {hotel['name']} attempt {attempt+1}")
        except Exception as e:
            logger.error(f"  Error scraping {hotel['name']}: {e}")

        if attempt < MAX_RETRIES:
            await page.wait_for_timeout(random.randint(3000, 6000))

    return results


def _parse_price(text: str) -> float | None:
    """Extract numeric price from text like '$342', '$1,204', '342 USD'."""
    import re
    text = text.replace(",", "").replace(" ", "")
    match = re.search(r"\$?([\d]+(?:\.\d{1,2})?)", text)
    if match:
        val = float(match.group(1))
        if 10 < val < 100_000:  # sanity bounds
            return val
    return None


# ── Main scraper loop ───────────────────────────────────────────────────────────

async def run_hotel_scraper(trip_id_filter: str = None, dry_run: bool = False):
    run_id = str(uuid_lib.uuid4())[:8]
    logger.info(f"=== Hotel Scraper run [{run_id}] started at {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    trips = get_active_trips()
    if trip_id_filter:
        trips = [t for t in trips if str(t["id"]) == trip_id_filter]

    if not trips:
        logger.warning("No active trips found. Add trips via Supabase or hotels.json loader.")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        for trip in trips:
            logger.info(f"\n── Trip: {trip['name']} ──────────────────────────────")
            hotels = get_trip_hotels(str(trip["id"]))
            date_combos = expand_date_combos(
                trip["check_in_start"],
                trip["check_in_end"],
                trip["durations"],
            )
            logger.info(f"  {len(hotels)} hotels × {len(date_combos)} date combos = "
                        f"{len(hotels) * len(date_combos)} searches")

            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            # Stealth: hide webdriver flag
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
            """)

            page = await context.new_page()

            for hotel in hotels:
                for check_in, check_out in date_combos:
                    results = await scrape_google_hotels(
                        page, hotel, check_in, check_out, trip["adults"]
                    )

                    for r in results:
                        if dry_run:
                            print(f"  [DRY RUN] {hotel['name']} | {check_in} → {check_out} | "
                                  f"{r['provider']} | ${r['price']:.2f}")
                            continue

                        snap_id = save_hotel_snapshot(
                            hotel_id=str(hotel["id"]),
                            trip_id=str(trip["id"]),
                            provider=r["provider"],
                            check_in=check_in,
                            check_out=check_out,
                            price_total=r["price"],
                            room_type=r.get("room_type"),
                            cancellable=r.get("cancellable"),
                            run_id=run_id,
                        )

                        is_new_low = upsert_price_low(
                            hotel_id=str(hotel["id"]),
                            provider=r["provider"],
                            check_in=check_in,
                            check_out=check_out,
                            new_price=r["price"],
                            snapshot_id=snap_id,
                        )

                        if is_new_low:
                            logger.info(f"  🔻 NEW LOW: {hotel['name']} "
                                        f"{check_in}→{check_out} | {r['provider']} | ${r['price']:.0f}")

                    # Polite delay between requests
                    sleep_secs = random.uniform(*REQUEST_DELAY)
                    await page.wait_for_timeout(int(sleep_secs * 1000))

            await context.close()

        await browser.close()

    logger.info(f"\n=== Hotel Scraper run [{run_id}] complete ===")


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hotel price scraper")
    parser.add_argument("--trip-id", help="Only scrape a specific trip UUID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without saving to database")
    args = parser.parse_args()

    asyncio.run(run_hotel_scraper(
        trip_id_filter=args.trip_id,
        dry_run=args.dry_run,
    ))
