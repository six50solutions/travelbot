"""
bot/price_check.py
On-demand price checker — run manually or via GitHub Actions workflow_dispatch.
Lets you check a specific hotel, trip, or date without waiting for the next cron run.

Usage:
    # Check all hotels for a trip on specific dates
    python bot/price_check.py --trip-id <uuid> --check-in 2026-08-10 --nights 3

    # Check a specific hotel across all its trip dates
    python bot/price_check.py --hotel "The Langham Chicago"

    # Show current best prices for a trip (no scraping, just DB query)
    python bot/price_check.py --trip-id <uuid> --summary

    # Show all current historic lows across every hotel
    python bot/price_check.py --lows

    # Show cheapest upcoming dates for a hotel
    python bot/price_check.py --hotel "The Langham Chicago" --cheapest

    # Add/update an alert threshold for a hotel
    python bot/price_check.py --set-threshold --hotel "The Langham Chicago" --price 250
"""

import sys
import os
import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_conn, get_active_hotels, get_active_trips
from scrapers.hotel_scraper import scrape_google_hotels, expand_date_combos
from scrapers.flight_scraper import scrape_google_flights
from utils.db import save_hotel_snapshot, upsert_price_low

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── DB Queries ─────────────────────────────────────────────────────────────────

def get_hotel_by_name(name: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM hotels
                WHERE LOWER(name) LIKE LOWER(%s) AND active = TRUE
                LIMIT 1
            """, (f"%{name}%",))
            row = cur.fetchone()
            return dict(row) if row else None


def get_trip_by_id(trip_id: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM trips WHERE id = %s", (trip_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_current_lows_for_hotel(hotel_id: str, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pl.*, h.name as hotel_name
                FROM price_lows pl
                JOIN hotels h ON h.id = pl.hotel_id
                WHERE pl.hotel_id = %s
                ORDER BY pl.check_in ASC, pl.low_price ASC
                LIMIT %s
            """, (hotel_id, limit))
            return [dict(r) for r in cur.fetchall()]


def get_all_lows(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pl.*, h.name as hotel_name
                FROM price_lows pl
                JOIN hotels h ON h.id = pl.hotel_id
                WHERE pl.check_in >= CURRENT_DATE
                ORDER BY pl.low_price ASC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


def get_trip_summary(trip_id: str) -> list[dict]:
    """Best current price per hotel for a trip."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    h.name as hotel_name,
                    pl.check_in,
                    pl.check_out,
                    pl.provider,
                    pl.low_price,
                    pl.recorded_at
                FROM price_lows pl
                JOIN trip_hotels th ON th.hotel_id = pl.hotel_id
                JOIN hotels h ON h.id = pl.hotel_id
                WHERE th.trip_id = %s
                  AND pl.check_in >= CURRENT_DATE
                ORDER BY pl.low_price ASC
                LIMIT 30
            """, (trip_id,))
            return [dict(r) for r in cur.fetchall()]


def get_cheapest_dates_for_hotel(hotel_id: str, top_n: int = 10) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    pl.check_in,
                    pl.check_out,
                    pl.provider,
                    pl.low_price,
                    (pl.check_out - pl.check_in) as nights,
                    pl.low_price / NULLIF((pl.check_out - pl.check_in), 0) as per_night
                FROM price_lows pl
                WHERE pl.hotel_id = %s
                  AND pl.check_in >= CURRENT_DATE
                ORDER BY pl.low_price ASC
                LIMIT %s
            """, (hotel_id, top_n))
            return [dict(r) for r in cur.fetchall()]


def get_flight_lows_for_trip(trip_id: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT fl.*, t.name as trip_name
                FROM flight_lows fl
                JOIN trips t ON t.id = fl.trip_id
                WHERE fl.trip_id = %s
                  AND fl.depart_date >= CURRENT_DATE
                ORDER BY fl.low_price ASC
                LIMIT 20
            """, (trip_id,))
            return [dict(r) for r in cur.fetchall()]


def set_threshold(hotel_id: str, price: float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Deactivate old threshold for this hotel
            cur.execute("""
                UPDATE alert_thresholds SET active = FALSE
                WHERE hotel_id = %s
            """, (hotel_id,))
            # Insert new one
            cur.execute("""
                INSERT INTO alert_thresholds (hotel_id, threshold_price)
                VALUES (%s, %s)
            """, (hotel_id, price))


# ── Display helpers ─────────────────────────────────────────────────────────────

def divider(label=""):
    width = 70
    if label:
        print(f"\n{'─' * 3} {label} {'─' * max(0, width - len(label) - 5)}")
    else:
        print("─" * width)


def print_lows_table(rows: list[dict], title="Historic Lows"):
    divider(title)
    if not rows:
        print("  No data yet.")
        return
    print(f"  {'Hotel':<30} {'Check-in':<12} {'Check-out':<12} {'Nights':<7} {'Provider':<16} {'Low Price':>10}")
    divider()
    for r in rows:
        nights = (r["check_out"] - r["check_in"]).days if hasattr(r.get("check_out"), "days") else "?"
        print(f"  {r.get('hotel_name','—'):<30} "
              f"{str(r['check_in']):<12} "
              f"{str(r['check_out']):<12} "
              f"{str(nights):<7} "
              f"{r.get('provider','—'):<16} "
              f"  ${float(r.get('low_price', r.get('price', 0))):.0f}")


def print_flight_table(rows: list[dict], title="Flight Lows"):
    divider(title)
    if not rows:
        print("  No flight data yet.")
        return
    print(f"  {'Route':<22} {'Depart':<12} {'Return':<12} {'Low Price':>10}")
    divider()
    for r in rows:
        route = f"{r['origin']} → {r['destination']}"
        print(f"  {route:<22} {str(r['depart_date']):<12} {str(r.get('return_date','—')):<12}   ${float(r['low_price']):.0f}")


# ── On-demand scrape ────────────────────────────────────────────────────────────

async def check_hotel_now(hotel: dict, check_in: date, check_out: date, adults: int = 2):
    from playwright.async_api import async_playwright
    import random

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    ]

    print(f"\nChecking: {hotel['name']} | {check_in} → {check_out}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        results = await scrape_google_hotels(page, hotel, check_in, check_out, adults)
        await browser.close()

    if not results:
        print("  ⚠  No prices found.")
        return

    print(f"\n  {'Provider':<20} {'Price':>10}  {'Cancellable'}")
    print("  " + "─" * 42)
    for r in sorted(results, key=lambda x: x["price"]):
        cancel = "✓ free cancel" if r.get("cancellable") else ""
        print(f"  {r['provider']:<20} ${r['price']:>8.0f}  {cancel}")

    # Save to DB
    for r in results:
        snap_id = save_hotel_snapshot(
            hotel_id=str(hotel["id"]),
            trip_id=None,
            provider=r["provider"],
            check_in=check_in,
            check_out=check_out,
            price_total=r["price"],
            room_type=r.get("room_type"),
            cancellable=r.get("cancellable"),
            run_id="manual",
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
            print(f"  🔻 NEW HISTORIC LOW: {r['provider']} ${r['price']:.0f}")


# ── Main CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Travel Tracker — On-demand price check bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Live price check for a hotel on specific dates
  python bot/price_check.py --hotel "Langham" --check-in 2026-08-10 --nights 3

  # Show trip summary (best prices per hotel, from DB)
  python bot/price_check.py --trip-id <uuid> --summary

  # Show all upcoming historic lows
  python bot/price_check.py --lows

  # Show cheapest dates for a hotel
  python bot/price_check.py --hotel "Langham" --cheapest

  # Set a price alert threshold
  python bot/price_check.py --set-threshold --hotel "Langham" --price 250

  # List all active trips
  python bot/price_check.py --list-trips

  # List all tracked hotels
  python bot/price_check.py --list-hotels
        """
    )

    parser.add_argument("--hotel",          help="Hotel name (partial match OK)")
    parser.add_argument("--trip-id",        help="Trip UUID")
    parser.add_argument("--check-in",       help="Check-in date YYYY-MM-DD")
    parser.add_argument("--nights",         type=int, help="Number of nights")
    parser.add_argument("--summary",        action="store_true", help="Show trip price summary from DB")
    parser.add_argument("--lows",           action="store_true", help="Show all current historic lows")
    parser.add_argument("--cheapest",       action="store_true", help="Show cheapest dates for a hotel")
    parser.add_argument("--set-threshold",  action="store_true", help="Set a price alert threshold")
    parser.add_argument("--price",          type=float, help="Threshold price for --set-threshold")
    parser.add_argument("--list-trips",     action="store_true", help="List all active trips")
    parser.add_argument("--list-hotels",    action="store_true", help="List all tracked hotels")

    args = parser.parse_args()

    # ── List trips ──────────────────────────────────────────────────────────
    if args.list_trips:
        trips = get_active_trips()
        divider("Active Trips")
        for t in trips:
            origin = f"{t['origin']} → " if t.get("origin") else ""
            print(f"  [{str(t['id'])[:8]}...]  {t['name']}")
            print(f"           {origin}{t['destination']} | "
                  f"{t['check_in_start']} → {t['check_in_end']} | "
                  f"Durations: {t['durations']} nights")
        return

    # ── List hotels ─────────────────────────────────────────────────────────
    if args.list_hotels:
        hotels = get_active_hotels()
        divider("Tracked Hotels")
        for h in hotels:
            tags = ", ".join(h.get("tags") or [])
            print(f"  {h['name']:<35} {h['location']:<20} {tags}")
        return

    # ── Show all lows ────────────────────────────────────────────────────────
    if args.lows:
        rows = get_all_lows()
        print_lows_table(rows, f"All Upcoming Historic Lows (top {len(rows)})")
        return

    # ── Trip summary ─────────────────────────────────────────────────────────
    if args.trip_id and args.summary:
        trip = get_trip_by_id(args.trip_id)
        if not trip:
            print(f"Trip not found: {args.trip_id}")
            return
        rows = get_trip_summary(args.trip_id)
        print_lows_table(rows, f"Trip Summary: {trip['name']}")
        flights = get_flight_lows_for_trip(args.trip_id)
        if flights:
            print_flight_table(flights, f"Flight Lows: {trip['name']}")
        return

    # ── Hotel-specific queries ───────────────────────────────────────────────
    if args.hotel:
        hotel = get_hotel_by_name(args.hotel)
        if not hotel:
            print(f"Hotel not found matching: '{args.hotel}'")
            print("Run --list-hotels to see tracked hotels.")
            return

        # Set threshold
        if args.set_threshold:
            if not args.price:
                print("--price required with --set-threshold")
                return
            set_threshold(str(hotel["id"]), args.price)
            print(f"✅ Threshold set: {hotel['name']} → alert below ${args.price:.0f}")
            return

        # Cheapest dates from DB
        if args.cheapest:
            rows = get_cheapest_dates_for_hotel(str(hotel["id"]))
            divider(f"Cheapest Upcoming Dates: {hotel['name']}")
            if not rows:
                print("  No price history yet. Run the scraper first.")
                return
            print(f"  {'Check-in':<12} {'Check-out':<12} {'Nights':<7} {'Per Night':>10} {'Total':>10}  {'Provider'}")
            print("  " + "─" * 65)
            for r in rows:
                per_night = float(r["per_night"]) if r.get("per_night") else 0
                print(f"  {str(r['check_in']):<12} {str(r['check_out']):<12} "
                      f"{r['nights']:<7} ${per_night:>8.0f} ${float(r['low_price']):>8.0f}  {r['provider']}")
            return

        # Live price check
        if args.check_in and args.nights:
            check_in  = date.fromisoformat(args.check_in)
            check_out = check_in + timedelta(days=args.nights)
            asyncio.run(check_hotel_now(hotel, check_in, check_out))
            return

        # Default: show existing lows for this hotel
        rows = get_current_lows_for_hotel(str(hotel["id"]))
        print_lows_table(rows, f"Historic Lows: {hotel['name']}")
        return

    # ── No args: show quick summary ──────────────────────────────────────────
    parser.print_help()
    print("\n── Quick Stats ────────────────────────────────────────────────────")
    hotels = get_active_hotels()
    trips  = get_active_trips()
    lows   = get_all_lows(limit=5)
    print(f"  Hotels tracked:   {len(hotels)}")
    print(f"  Active trips:     {len(trips)}")
    print(f"\n  Top 5 Current Deals:")
    if lows:
        for r in lows:
            nights = (r["check_out"] - r["check_in"]).days
            print(f"    ${float(r['low_price']):<8.0f} {r['hotel_name']} | "
                  f"{r['check_in']} ({nights}n) | {r['provider']}")
    else:
        print("    No price history yet — run the scraper first.")


if __name__ == "__main__":
    main()
