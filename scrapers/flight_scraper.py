"""
scrapers/flight_scraper.py
Scrapes Google Flights via Playwright for trips that have an origin defined.
Saves results to flight_snapshots + updates flight_lows.

Usage:
    python scrapers/flight_scraper.py
    python scrapers/flight_scraper.py --trip-id <uuid>
    python scrapers/flight_scraper.py --dry-run
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
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import (
    get_active_trips, save_flight_snapshot, upsert_flight_low
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

HEADLESS      = True
REQUEST_DELAY = (5, 10)
MAX_RETRIES   = 2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


# ── URL builder ─────────────────────────────────────────────────────────────────

def build_google_flights_url(origin: str, destination: str,
                              depart_date: date, return_date: date = None,
                              adults: int = 2) -> str:
    """
    Google Flights URL for round-trip or one-way.
    Uses the search URL format that pre-fills origin/destination/dates.
    """
    dep = depart_date.strftime("%Y-%m-%d")
    if return_date:
        ret = return_date.strftime("%Y-%m-%d")
        trip_type = "2"  # round trip
    else:
        ret = ""
        trip_type = "3"  # one way

    # Google Flights URL format
    # tfs= encodes the full trip in a compact format
    # Simpler approach: use the query params that work in browser
    base = "https://www.google.com/travel/flights"
    params = (
        f"?q=Flights+from+{quote_plus(origin)}+to+{quote_plus(destination)}"
        f"&hl=en&gl=us&curr=USD"
        f"&tfs=CBwQAhoeEgoyMDI2LTA4LTAxagcIARIDT1JEcgcIARIDTEFYGh4SCjIwMjYtMDgtMDVqBwgBEgNMQVhyBwgBEgNPUkQ"
    )
    # Better: direct search URL
    direct = (
        f"https://www.google.com/flights#flt="
        f"{origin}.{destination}.{dep}"
    )
    if return_date:
        direct += f"*{destination}.{origin}.{ret}"
    direct += f";c:USD;e:1;sd:1;t:{'r' if return_date else 'o'}"

    return direct


def build_flights_search_url(origin: str, destination: str,
                              depart_date: date, return_date: date = None) -> str:
    """Alternative simpler Google Flights URL via travel search."""
    dep = depart_date.strftime("%Y-%m-%d")
    url = (
        f"https://www.google.com/travel/flights/search"
        f"?tfs=CBwQAhoeagcIARIDORD&hl=en&curr=USD"
    )
    # Most reliable: construct the full query URL
    dep_str = depart_date.strftime("%Y-%m-%d")
    q = f"flights from {origin} to {destination} on {dep_str}"
    if return_date:
        ret_str = return_date.strftime("%Y-%m-%d")
        q += f" returning {ret_str}"
    return f"https://www.google.com/search?q={quote_plus(q)}&hl=en"


# ── Scraper ─────────────────────────────────────────────────────────────────────

async def scrape_google_flights(page, trip: dict, depart_date: date,
                                 return_date: date = None) -> list[dict]:
    """
    Scrape Google Flights for a specific route + dates.
    Returns list of {airline, price, stops, duration_mins, provider}
    """
    origin = trip.get("origin", "").upper()
    destination = trip.get("destination", "")

    if not origin:
        logger.warning(f"Trip '{trip['name']}' has no origin — skipping flights")
        return []

    # Try the Google search result cards first (more scraping-friendly)
    url = build_flights_search_url(origin, destination, depart_date, return_date)
    results = []

    for attempt in range(MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(random.randint(3000, 5000))

            # ── Accept consent if shown ──────────────────────────────────────
            try:
                consent = page.locator('button:has-text("Accept all"), [aria-label*="Accept"]')
                if await consent.count() > 0:
                    await consent.first.click()
                    await page.wait_for_timeout(1500)
            except Exception:
                pass

            # ── Strategy 1: Google Flights widget in search results ──────────
            # The rich result card shows flight prices
            price_elements = page.locator(
                '[data-price], [aria-label*="$"], '
                '.YMlIz, .U3gSDe, [jscontroller="iSvg6e"]'
            )
            count = await price_elements.count()

            if count == 0:
                # ── Strategy 2: Navigate directly to Google Flights ──────────
                flights_url = build_google_flights_url(
                    origin, destination, depart_date, return_date
                )
                await page.goto(flights_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(random.randint(4000, 7000))

                # Wait for flight list to populate
                try:
                    await page.wait_for_selector(
                        'li[class*="flight"], [data-ved][role="listitem"], '
                        '[jsname="t7vRVb"], .pIav2d',
                        timeout=15_000
                    )
                except PWTimeout:
                    logger.warning(f"  Flights list timeout for {origin}→{destination} {depart_date}")

                price_elements = page.locator(
                    '[data-price], [aria-label*="$"], '
                    '.YMlIz, .U3gSDe, [jsname="cfb5o"], '
                    'div[class*="price"] span'
                )
                count = await price_elements.count()

            # ── Extract from flight result rows ──────────────────────────────
            flight_rows = page.locator(
                'li[class*="flight"], [data-ved][role="listitem"], '
                '[jsname="t7vRVb"], .pIav2d, [data-ved] li'
            )
            row_count = await flight_rows.count()

            for i in range(min(row_count, 8)):
                try:
                    row = flight_rows.nth(i)
                    row_text = await row.inner_text()

                    price = _parse_flight_price(row_text)
                    if not price:
                        continue

                    # Airline
                    airline = _extract_airline(row_text)

                    # Stops
                    stops = 0
                    if "nonstop" in row_text.lower():
                        stops = 0
                    elif "1 stop" in row_text.lower():
                        stops = 1
                    elif "2 stop" in row_text.lower():
                        stops = 2

                    # Duration
                    duration = _parse_duration(row_text)

                    results.append({
                        "airline": airline,
                        "price": price,
                        "stops": stops,
                        "duration_mins": duration,
                        "provider": "Google Flights",
                    })
                except Exception as e:
                    logger.debug(f"Row parse error: {e}")

            # Fallback: grab price elements directly
            if not results and count > 0:
                seen_prices = set()
                for i in range(min(count, 5)):
                    try:
                        el = price_elements.nth(i)
                        text = await el.inner_text()
                        price = _parse_flight_price(text)
                        if price and price not in seen_prices:
                            seen_prices.add(price)
                            results.append({
                                "airline": "Unknown",
                                "price": price,
                                "stops": None,
                                "duration_mins": None,
                                "provider": "Google Flights",
                            })
                    except Exception:
                        pass

            if results:
                logger.info(
                    f"  ✓ {origin}→{destination} | {depart_date} | "
                    f"{len(results)} flight(s) found | "
                    f"lowest ${min(r['price'] for r in results):.0f}"
                )
                break

            logger.warning(f"  ⚠ No flights found for {origin}→{destination} {depart_date}, attempt {attempt+1}")

        except PWTimeout:
            logger.warning(f"  Timeout for {origin}→{destination} attempt {attempt+1}")
        except Exception as e:
            logger.error(f"  Error scraping flights {origin}→{destination}: {e}")

        if attempt < MAX_RETRIES:
            await page.wait_for_timeout(random.randint(4000, 8000))

    return results


def _parse_flight_price(text: str) -> float | None:
    import re
    text = text.replace(",", "").replace(" ", "")
    # Flight prices are typically $X,XXX — look for $nnn+ patterns
    match = re.search(r"\$(\d{2,5}(?:\.\d{1,2})?)", text)
    if match:
        val = float(match.group(1))
        if 30 < val < 50_000:
            return val
    return None


def _extract_airline(text: str) -> str:
    known = [
        "United", "Delta", "American", "Southwest", "JetBlue",
        "Alaska", "Spirit", "Frontier", "Allegiant", "Hawaiian",
        "Air Canada", "Lufthansa", "British Airways", "Emirates",
    ]
    for airline in known:
        if airline.lower() in text.lower():
            return airline
    return "Unknown"


def _parse_duration(text: str) -> int | None:
    import re
    # "5 hr 20 min" or "5h 20m"
    match = re.search(r"(\d+)\s*hr?\s*(\d+)?\s*min?", text, re.IGNORECASE)
    if match:
        hours = int(match.group(1))
        mins = int(match.group(2)) if match.group(2) else 0
        return hours * 60 + mins
    return None


# ── Main ────────────────────────────────────────────────────────────────────────

async def run_flight_scraper(trip_id_filter: str = None, dry_run: bool = False):
    run_id = str(uuid_lib.uuid4())[:8]
    logger.info(f"=== Flight Scraper run [{run_id}] started at {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    trips = [t for t in get_active_trips() if t.get("origin")]
    if trip_id_filter:
        trips = [t for t in trips if str(t["id"]) == trip_id_filter]

    if not trips:
        logger.info("No trips with origin defined — skipping flight scraper")
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
            logger.info(f"\n── Trip: {trip['name']} [{trip['origin']} → {trip['destination']}] ──")

            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()

            # Generate depart dates from trip window
            check_in_start = trip["check_in_start"]
            check_in_end   = trip["check_in_end"]

            current = check_in_start
            while current <= check_in_end:
                for dur in trip["durations"]:
                    return_date = current + timedelta(days=dur)
                    results = await scrape_google_flights(page, trip, current, return_date)

                    for r in results:
                        if dry_run:
                            print(f"  [DRY RUN] {trip['origin']}→{trip['destination']} | "
                                  f"{current} / Return {return_date} | "
                                  f"{r['airline']} | ${r['price']:.0f} | {r['stops']} stop(s)")
                            continue

                        snap_id = save_flight_snapshot(
                            trip_id=str(trip["id"]),
                            origin=trip["origin"],
                            destination=trip["destination"],
                            depart_date=current,
                            return_date=return_date,
                            price=r["price"],
                            airline=r.get("airline"),
                            stops=r.get("stops", 0),
                            duration_mins=r.get("duration_mins"),
                            provider=r.get("provider", "Google Flights"),
                            run_id=run_id,
                        )

                        is_new_low = upsert_flight_low(
                            trip_id=str(trip["id"]),
                            origin=trip["origin"],
                            destination=trip["destination"],
                            depart_date=current,
                            return_date=return_date,
                            new_price=r["price"],
                            snapshot_id=snap_id,
                        )

                        if is_new_low:
                            logger.info(f"  🔻 NEW FLIGHT LOW: {trip['origin']}→{trip['destination']} "
                                        f"{current}/ret {return_date} | "
                                        f"{r['airline']} | ${r['price']:.0f}")

                    await page.wait_for_timeout(int(random.uniform(*REQUEST_DELAY) * 1000))

                current += timedelta(days=1)

            await context.close()

        await browser.close()

    logger.info(f"\n=== Flight Scraper run [{run_id}] complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Flight price scraper")
    parser.add_argument("--trip-id", help="Scrape only a specific trip UUID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(run_flight_scraper(
        trip_id_filter=args.trip_id,
        dry_run=args.dry_run,
    ))
