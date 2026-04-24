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
    from urllib.parse import quote_plus
    q = quote_plus(search_query)
    ci = check_in.strftime("%Y-%m-%d")
    co = check_out.strftime("%Y-%m-%d")
    # Use the /search endpoint which is more stable
    return (
        f"https://www.google.com/travel/hotels/search"
        f"?q={q}&checkin={ci}&checkout={co}&adults={adults}&hl=en&gl=us&curr=USD"
    )


DEBUG_SCREENSHOTS = os.environ.get("DEBUG_SCREENSHOTS", "0") == "1"


async def scrape_google_hotels(page, hotel: dict, check_in: date,
                                check_out: date, adults: int = 2) -> list[dict]:
    """
    Navigate to Google Hotels for a specific hotel + dates.
    Returns list of {provider, price, room_type, cancellable}
    """
    url = build_google_hotels_url(hotel["search_query"], check_in, check_out, adults)
    results = []

    for attempt in range(MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            await page.wait_for_timeout(random.randint(2500, 4500))

            # ── Dismiss consent / cookie banner ─────────────────────────────
            for consent_text in ["Accept all", "I agree", "Agree", "Accept"]:
                try:
                    btn = page.locator(f'button:has-text("{consent_text}")')
                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # ── Debug: save screenshot to see what we're working with ───────
            if DEBUG_SCREENSHOTS:
                safe_name = hotel["name"].replace(" ", "_")[:30]
                await page.screenshot(
                    path=f"debug_{safe_name}_{check_in}.png",
                    full_page=False
                )
                logger.info(f"  📸 Screenshot saved: debug_{safe_name}_{check_in}.png")

            # ── Strategy 1: look for hotel card in search results list ───────
            # Google Hotels search returns a list — find our hotel and click it
            try:
                # Wait for hotel cards to appear
                await page.wait_for_selector(
                    '[data-hotelid], [jsname="aXgaGb"], .BcKAgd, [data-ved] h2, .OSrXXb',
                    timeout=10_000
                )
            except PWTimeout:
                pass

            # Click first hotel result (should match our search query)
            clicked = False
            for card_sel in [
                '[data-hotelid]',
                '[jsname="aXgaGb"]',
                '.BcKAgd',
                'li[data-ved]',
                '[role="article"]',
            ]:
                try:
                    card = page.locator(card_sel).first
                    if await card.count() > 0:
                        await card.click()
                        await page.wait_for_timeout(2500)
                        clicked = True
                        break
                except Exception:
                    pass

            # ── Strategy 2: extract prices from page ─────────────────────────
            # Try multiple selector patterns Google has used
            price_selectors = [
                # Prices panel / booking options
                '[jsname="priceRow"]',
                '[data-price]',
                '.kCsRcb',
                '[jscontroller="rTuANe"]',
                # Provider rows in rates panel
                '.GZnc6d',
                '[data-provider]',
                # Rate cards
                '.vQlnEc',
                '[jsname="MfMn2b"]',
                # Generic price spans
                'span[aria-label*="$"]',
                'div[aria-label*="$"]',
            ]

            for sel in price_selectors:
                try:
                    els = page.locator(sel)
                    count = await els.count()
                    if count == 0:
                        continue

                    for i in range(min(count, 10)):
                        try:
                            el = els.nth(i)
                            text = await el.inner_text()
                            price = _parse_price(text)
                            if not price:
                                continue

                            # Try to extract provider name from nearby elements
                            provider = "Google Hotels"
                            for prov_sel in [
                                '[data-provider-name]', '.vQlnEc', '.GZnc6d',
                                'span[class*="provider"]', 'div[class*="partner"]'
                            ]:
                                try:
                                    prov = el.locator(prov_sel).first
                                    if await prov.count() > 0:
                                        pname = (await prov.inner_text()).strip()
                                        if pname and len(pname) < 40:
                                            provider = pname
                                            break
                                except Exception:
                                    pass

                            # Dedup by price+provider
                            if not any(
                                r["price"] == price and r["provider"] == provider
                                for r in results
                            ):
                                results.append({
                                    "provider": provider,
                                    "price": price,
                                    "room_type": None,
                                    "cancellable": None,
                                })
                        except Exception:
                            pass

                    if results:
                        break
                except Exception:
                    pass

            # ── Strategy 3: full page text scrape as last resort ─────────────
            if not results:
                try:
                    full_text = await page.inner_text("body")
                    import re
                    # Find all dollar amounts on the page
                    prices_found = re.findall(r'\$\s*(\d{2,4}(?:,\d{3})?)', full_text)
                    for p_str in prices_found[:5]:
                        price = float(p_str.replace(",", ""))
                        if 50 < price < 20_000:
                            results.append({
                                "provider": "Google Hotels",
                                "price": price,
                                "room_type": None,
                                "cancellable": None,
                            })
                            break  # just take the first reasonable price
                except Exception:
                    pass

            if results:
                logger.info(
                    f"  ✓ {hotel['name']} | {check_in} → {check_out} | "
                    f"{len(results)} price(s) | "
                    f"lowest ${min(r['price'] for r in results):.0f}"
                )
                break

            logger.warning(
                f"  ⚠ No prices found for {hotel['name']} ({check_in}), attempt {attempt+1}"
            )

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
