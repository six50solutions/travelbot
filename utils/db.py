"""
utils/db.py — Supabase/Postgres connection helper
Uses psycopg2 directly for full SQL control.
"""

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime
import uuid
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["SUPABASE_DB_URL"]
# Format: postgresql://postgres:[password]@db.[project-ref].supabase.co:5432/postgres


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_active_hotels() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM hotels WHERE active = TRUE ORDER BY name")
            return [dict(r) for r in cur.fetchall()]


def get_active_trips() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM trips WHERE active = TRUE")
            return [dict(r) for r in cur.fetchall()]


def get_trip_hotels(trip_id: str) -> list[dict]:
    """Returns hotels linked to a trip, or ALL hotels if none are linked."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT h.* FROM hotels h
                JOIN trip_hotels th ON th.hotel_id = h.id
                WHERE th.trip_id = %s AND h.active = TRUE
            """, (trip_id,))
            rows = cur.fetchall()
            if rows:
                return [dict(r) for r in rows]
            # Fallback: return all active hotels
            cur.execute("SELECT * FROM hotels WHERE active = TRUE ORDER BY name")
            return [dict(r) for r in cur.fetchall()]


def save_hotel_snapshot(
    hotel_id: str,
    trip_id: str,
    provider: str,
    check_in,
    check_out,
    price_total: float,
    currency: str = "USD",
    room_type: str = None,
    cancellable: bool = None,
    run_id: str = None,
) -> int:
    nights = (check_out - check_in).days
    price_per_night = round(price_total / nights, 2) if nights > 0 else None

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO price_snapshots
                    (hotel_id, trip_id, provider, check_in, check_out,
                     price_total, price_per_night, currency, room_type, cancellable, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (hotel_id, trip_id, provider, check_in, check_out,
                  price_total, price_per_night, currency, room_type, cancellable, run_id))
            return cur.fetchone()[0]


def save_flight_snapshot(
    trip_id: str,
    origin: str,
    destination: str,
    depart_date,
    return_date,
    price: float,
    airline: str = None,
    stops: int = 0,
    duration_mins: int = None,
    provider: str = "Google Flights",
    run_id: str = None,
) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO flight_snapshots
                    (trip_id, origin, destination, depart_date, return_date,
                     price, airline, stops, duration_mins, provider, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (trip_id, origin, destination, depart_date, return_date,
                  price, airline, stops, duration_mins, provider, run_id))
            return cur.fetchone()[0]


def get_current_low(hotel_id: str, provider: str, check_in, check_out) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM price_lows
                WHERE hotel_id = %s AND provider = %s
                  AND check_in = %s AND check_out = %s
            """, (hotel_id, provider, check_in, check_out))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_price_low(hotel_id: str, provider: str, check_in, check_out,
                     new_price: float, snapshot_id: int) -> bool:
    """Returns True if this is a new historic low."""
    existing = get_current_low(hotel_id, provider, check_in, check_out)

    if existing and existing["low_price"] <= new_price:
        return False  # Not a new low

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO price_lows
                    (hotel_id, provider, check_in, check_out, low_price, snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (hotel_id, provider, check_in, check_out)
                DO UPDATE SET
                    low_price = EXCLUDED.low_price,
                    snapshot_id = EXCLUDED.snapshot_id,
                    recorded_at = NOW()
            """, (hotel_id, provider, check_in, check_out, new_price, snapshot_id))
    return True


def get_current_flight_low(trip_id: str, origin: str, destination: str,
                            depart_date, return_date) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM flight_lows
                WHERE trip_id = %s AND origin = %s AND destination = %s
                  AND depart_date = %s AND return_date = %s
            """, (trip_id, origin, destination, depart_date, return_date))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_flight_low(trip_id: str, origin: str, destination: str,
                      depart_date, return_date, new_price: float,
                      snapshot_id: int) -> bool:
    existing = get_current_flight_low(trip_id, origin, destination, depart_date, return_date)
    if existing and existing["low_price"] <= new_price:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO flight_lows
                    (trip_id, origin, destination, depart_date, return_date, low_price, snapshot_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (trip_id, origin, destination, depart_date, return_date)
                DO UPDATE SET
                    low_price = EXCLUDED.low_price,
                    snapshot_id = EXCLUDED.snapshot_id,
                    recorded_at = NOW()
            """, (trip_id, origin, destination, depart_date, return_date, new_price, snapshot_id))
    return True


def log_alert(hotel_id, trip_id, snapshot_id, alert_type, price, prev_low=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alert_log
                    (hotel_id, trip_id, snapshot_id, alert_type, price, prev_low)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (hotel_id, trip_id, snapshot_id, alert_type, price, prev_low))


def get_rolling_avg(hotel_id: str, provider: str, check_in, check_out,
                    days_back: int = 30) -> float | None:
    """Rolling average price for a specific hotel/dates combo over last N days."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT AVG(price_total) FROM price_snapshots
                WHERE hotel_id = %s AND provider = %s
                  AND check_in = %s AND check_out = %s
                  AND scraped_at > NOW() - INTERVAL '%s days'
            """, (hotel_id, provider, check_in, check_out, days_back))
            result = cur.fetchone()[0]
            return float(result) if result else None


def get_alert_thresholds(hotel_id: str = None, trip_id: str = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if hotel_id:
                cur.execute("""
                    SELECT * FROM alert_thresholds
                    WHERE hotel_id = %s AND active = TRUE
                """, (hotel_id,))
            elif trip_id:
                cur.execute("""
                    SELECT * FROM alert_thresholds
                    WHERE trip_id = %s AND active = TRUE
                """, (trip_id,))
            else:
                cur.execute("SELECT * FROM alert_thresholds WHERE active = TRUE")
            return [dict(r) for r in cur.fetchall()]


def was_alerted_recently(hotel_id: str, check_in, check_out, hours: int = 12) -> bool:
    """Deduplication check — avoid re-alerting within the same scrape window."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM alert_log al
                JOIN price_snapshots ps ON ps.id = al.snapshot_id
                WHERE al.hotel_id = %s
                  AND ps.check_in = %s AND ps.check_out = %s
                  AND al.notified_at > NOW() - INTERVAL '%s hours'
                LIMIT 1
            """, (hotel_id, check_in, check_out, hours))
            return cur.fetchone() is not None
