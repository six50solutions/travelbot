"""
scripts/seed_from_config.py
One-time script to load hotels.json into your Supabase database.
Also seeds trips and links trip_hotels.

Usage:
    python scripts/seed_from_config.py
    python scripts/seed_from_config.py --config config/hotels.json
"""

import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def seed_hotels(hotels: list[dict]) -> dict[str, str]:
    """Insert hotels, return {name: uuid} mapping."""
    hotel_ids = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            for h in hotels:
                cur.execute("""
                    INSERT INTO hotels (name, location, search_query, tags, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id, name
                """, (
                    h["name"],
                    h["location"],
                    h["search_query"],
                    h.get("tags", []),
                    h.get("notes"),
                ))
                row = cur.fetchone()
                if row:
                    hotel_ids[h["name"]] = str(row[0])
                    logger.info(f"  ✓ Hotel: {h['name']}")
                else:
                    # Already exists — fetch the ID
                    cur.execute("SELECT id FROM hotels WHERE name = %s", (h["name"],))
                    existing = cur.fetchone()
                    if existing:
                        hotel_ids[h["name"]] = str(existing[0])
    return hotel_ids


def seed_trips(trips: list[dict]) -> dict[str, str]:
    """Insert trips, return {name: uuid} mapping."""
    trip_ids = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            for t in trips:
                cur.execute("""
                    INSERT INTO trips
                        (name, origin, destination, check_in_start, check_in_end,
                         durations, adults)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id, name
                """, (
                    t["name"],
                    t.get("origin"),
                    t["destination"],
                    t["check_in_start"],
                    t["check_in_end"],
                    t["durations"],
                    t.get("adults", 2),
                ))
                row = cur.fetchone()
                if row:
                    trip_ids[t["name"]] = str(row[0])
                    logger.info(f"  ✓ Trip: {t['name']}")
                else:
                    cur.execute("SELECT id FROM trips WHERE name = %s", (t["name"],))
                    existing = cur.fetchone()
                    if existing:
                        trip_ids[t["name"]] = str(existing[0])
    return trip_ids


def link_trip_hotels(trip_ids: dict, hotel_ids: dict, trip_configs: list[dict]):
    """Link all hotels to trips (or specific ones if 'hotels' key is in trip config)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            for t in trip_configs:
                trip_id = trip_ids.get(t["name"])
                if not trip_id:
                    continue

                # If trip config specifies hotels, use those; else link all
                target_hotels = t.get("hotels", list(hotel_ids.keys()))

                for hotel_name in target_hotels:
                    hotel_id = hotel_ids.get(hotel_name)
                    if hotel_id:
                        cur.execute("""
                            INSERT INTO trip_hotels (trip_id, hotel_id)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING
                        """, (trip_id, hotel_id))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/hotels.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    hotels = config.get("hotels", [])
    trips  = config.get("trips", [])

    logger.info(f"Seeding {len(hotels)} hotels and {len(trips)} trips...")

    logger.info("\n📍 Hotels:")
    hotel_ids = seed_hotels(hotels)

    logger.info("\n✈️  Trips:")
    trip_ids = seed_trips(trips)

    logger.info("\n🔗 Linking trips ↔ hotels...")
    link_trip_hotels(trip_ids, hotel_ids, trips)

    logger.info(f"\n✅ Seeded {len(hotel_ids)} hotels, {len(trip_ids)} trips.")
    logger.info("Run 'python scrapers/hotel_scraper.py --dry-run' to test.")


if __name__ == "__main__":
    main()
