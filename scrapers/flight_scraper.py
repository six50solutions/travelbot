"""
scrapers/flight_scraper.py
Scrapes Google Flights directly via Playwright using IATA codes.
Navigates to google.com/travel/flights with pre-filled origin/destination,
then extracts flight rows from the results page.

Usage:
    python scrapers/flight_scraper.py
    python scrapers/flight_scraper.py --trip-id <uuid>
    python scrapers/flight_scraper.py --dry-run
"""

import os
import sys
import re
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

HEADLESS     = True
MAX_RETRIES  = 2

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# IATA code map
DESTINATION_IATA = {
    "Maui, HI": "OGG",
    "Honolulu, HI": "HNL",
    "Miami Beach, FL": "MIA",
    "Miami, FL": "MIA",
    "Cancun, Mexico": "CUN",
    "Paris, France": "CDG",
    "London, UK": "LHR",
    "Barcelona, Spain": "BCN",
    "Rome, Italy": "FCO",
    "Amsterdam, Netherlands": "AMS",
    "Tokyo, Japan": "NRT",
    "Bangkok, Thailand": "BKK",
    "Singapore": "SIN",
    "Bali, Indonesia": "DPS",
    "Dubai, UAE": "DXB",
}

def get_iata(destination: str) -> str:
    return DESTINATION_IATA.get(destination, destination[:3].upper())


def build_google_flights_url(origin: str, destination: str,
                              depart_date: date, return_date: date = None) -> str:
    """
    Build Google Flights URL using natural language query.
    The page auto-resolves IATA codes and fills in the search form.
    """
    dst_iata = get_iata(destination)
    dep_str  = depart_date.strftime("%Y-%m-%d")

    if return_date:
        ret_str = return_date.strftime("%Y-%m-%d")
        q = f"Flights from {origin} to {dst_iata} on {dep_str} returning {ret_str}"
    else:
        q = f"Flights from {origin} to {dst_iata} on {dep_str}"

    return f"https://www.google.com/travel/flights?q={quote_plus(q)}&hl=en&curr=USD"


async def dismiss_modals(page):
    for text in ["Accept all", "I agree", "Agree", "Reject all"]:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass


async def scrape_google_flights(page, trip: dict, depart_date: date,
                                 return_date: date = None) -> list[dict]:
    """
    Scrape Google Flights for a specific route + dates.
    Returns list of {airline, price, stops, duration_mins}

    Based on observed DOM (Apr 2026):
    - Flight rows are list items inside 'Top departing flights' / 'Other departing flights'
    - Price: green span with $ amount, aria-label contains full price
    - Airline: text node near logo image
    - Stops: text like "1 stop" or "Nonstop"
    - Duration: text like "12 hr 26 min"
    """
    origin      = trip.get("origin", "").upper()
    destination = trip.get("destination", "")
    dst_iata    = get_iata(destination)
    results     = []

    url = build_google_flights_url(origin, destination, depart_date, return_date)

    for attempt in range(MAX_RETRIES + 1):
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            await page.wait_for_timeout(random.randint(3000, 5000))
            await dismiss_modals(page)
            await page.wait_for_timeout(1000)

            # Wait for flight results to appear
            try:
                await page.wait_for_selector(
                    'li[jsname], [jsname="IWWDBc"] li, ul[jsname] li',
                    timeout=12_000
                )
            except PWTimeout:
                pass

            # ── Extract flight rows via JavaScript ──────────────────────────
            # Walk the DOM looking for elements that contain both a price and
            # airline name — these are the flight result rows
            flight_data = await page.evaluate("""
                () => {
                    const results = [];

                    // Find all list items that look like flight rows
                    // They contain: airline name, time, duration, stops, price
                    const rows = document.querySelectorAll(
                        'li[jsname], [role="listitem"], ul li'
                    );

                    for (const row of rows) {
                        const text = row.innerText || '';
                        if (!text.includes('$')) continue;
                        if (text.length < 20 || text.length > 1000) continue;

                        // Must have a price pattern
                        const priceMatch = text.match(/\\$(\\d{3,5})/);
                        if (!priceMatch) continue;
                        const price = parseFloat(priceMatch[1]);
                        if (price < 150 || price > 20000) continue;

                        // Extract airline (first non-empty short line)
                        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                        let airline = 'Unknown';
                        for (const line of lines) {
                            if (line.length > 2 && line.length < 40 &&
                                !line.includes('$') &&
                                !line.match(/^\\d/) &&
                                !line.includes('stop') &&
                                !line.includes('hr') &&
                                !line.includes('min') &&
                                !line.includes('kg') &&
                                !line.includes('ORD') &&
                                !line.includes('emissions')) {
                                airline = line;
                                break;
                            }
                        }

                        // Extract stops
                        let stops = 0;
                        if (text.includes('Nonstop') || text.includes('nonstop')) {
                            stops = 0;
                        } else if (text.match(/2 stops?/i)) {
                            stops = 2;
                        } else if (text.match(/1 stop/i)) {
                            stops = 1;
                        }

                        // Extract duration
                        let duration_mins = null;
                        const durMatch = text.match(/(\\d+) hr (\\d+) min/);
                        if (durMatch) {
                            duration_mins = parseInt(durMatch[1]) * 60 + parseInt(durMatch[2]);
                        }

                        results.push({
                            price,
                            airline,
                            stops,
                            duration_mins,
                        });
                    }

                    // Deduplicate by price+airline
                    const seen = new Set();
                    return results.filter(r => {
                        const key = `${r.price}-${r.airline}`;
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    }).slice(0, 8);
                }
            """)

            if flight_data:
                for r in flight_data:
                    results.append({
                        "airline":      r.get("airline", "Unknown"),
                        "price":        float(r["price"]),
                        "stops":        r.get("stops", 0),
                        "duration_mins": r.get("duration_mins"),
                        "provider":     "Google Flights",
                    })

            if results:
                lowest = min(r["price"] for r in results)
                logger.info(
                    f"  ✓ {origin}→{dst_iata} | {depart_date} | "
                    f"{len(results)} flight(s) | lowest ${lowest:.0f}"
                )
                break

            # Fallback: direct aria-label price extraction
            price_els = page.locator('span[aria-label*="US dollars"], [aria-label*="round trip"]')
            count = await price_els.count()
            if count > 0:
                for i in range(min(count, 5)):
                    try:
                        label = await price_els.nth(i).get_attribute("aria-label") or ""
                        m = re.search(r'\$?([\d,]+)', label)
                        if m:
                            price = float(m.group(1).replace(",", ""))
                            if 150 < price < 20_000:
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
                    break

            logger.warning(
                f"  ⚠ No flights found for {origin}→{dst_iata} {depart_date}, "
                f"attempt {attempt + 1}"
            )

        except PWTimeout:
            logger.warning(f"  Timeout {origin}→{dst_iata} attempt {attempt + 1}")
        except Exception as e:
            logger.error(f"  Error {origin}→{dst_iata}: {e}")

        if attempt < MAX_RETRIES:
            await page.wait_for_timeout(random.randint(4000, 7000))

    return results


async def run_flight_scraper(trip_id_filter: str = None, dry_run: bool = False):
    run_id = str(uuid_lib.uuid4())[:8]
    logger.info(
        f"=== Flight Scraper run [{run_id}] started at "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} ==="
    )

    trips = [t for t in get_active_trips() if t.get("origin")]
    if trip_id_filter:
        trips = [t for t in trips if str(t["id"]) == trip_id_filter]

    if not trips:
        logger.info("No trips with origin defined — skipping")
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
            dst_iata = get_iata(trip["destination"])
            logger.info(
                f"\n── Trip: {trip['name']} "
                f"[{trip['origin']} → {dst_iata}] ──"
            )

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

            current = trip["check_in_start"]
            end     = trip["check_in_end"]

            while current <= end:
                for dur in trip["durations"]:
                    return_date = current + timedelta(days=dur)
                    results = await scrape_google_flights(
                        page, trip, current, return_date
                    )

                    for r in results:
                        if dry_run:
                            print(
                                f"  [DRY RUN] {trip['origin']}→{dst_iata} | "
                                f"{current} / ret {return_date} | "
                                f"{r['airline']} | ${r['price']:.0f} | "
                                f"{r['stops']} stop(s)"
                            )
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
                            provider="Google Flights",
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
                            logger.info(
                                f"  🔻 NEW LOW: {trip['origin']}→{dst_iata} "
                                f"{current}/ret {return_date} | "
                                f"{r['airline']} | ${r['price']:.0f}"
                            )

                    # Polite delay
                    await page.wait_for_timeout(random.randint(4000, 8000))

                current += timedelta(days=1)

            await context.close()

        await browser.close()

    logger.info(f"\n=== Flight Scraper [{run_id}] complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--trip-id", help="Scrape only a specific trip UUID")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(run_flight_scraper(
        trip_id_filter=args.trip_id,
        dry_run=args.dry_run,
    ))
