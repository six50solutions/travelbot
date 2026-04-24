"""
alerts/alert_engine.py
Queries price_snapshots from the most recent run, detects:
  1. Historic lows (new all-time low for hotel+dates+provider)
  2. Threshold breaches (price dropped below user-set fixed threshold)
  3. Percentage drops (price X% below rolling 30-day average)

Then sends a single digest email via Microsoft Graph.

Usage:
    python alerts/alert_engine.py
    python alerts/alert_engine.py --run-id <id>   # specific run only
    python alerts/alert_engine.py --dry-run        # print without emailing
"""

import sys
import os
import logging
import argparse
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import (
    get_conn, get_alert_thresholds, get_rolling_avg,
    was_alerted_recently, log_alert
)
from utils.graph_client import (
    send_alert_email,
    build_hotel_alert_html,
    build_flight_alert_html,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PCT_DROP_THRESHOLD = float(os.environ.get("ALERT_PCT_DROP", "10"))  # alert if 10%+ below avg


# ── Fetch recent snapshots ──────────────────────────────────────────────────────

def get_recent_hotel_snapshots(run_id: str = None, hours_back: int = 13) -> list[dict]:
    """Get hotel snapshots from the last run (or last N hours)."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if run_id:
                cur.execute("""
                    SELECT ps.*, h.name as hotel_name, h.id as hotel_id_val
                    FROM price_snapshots ps
                    JOIN hotels h ON h.id = ps.hotel_id
                    WHERE ps.run_id = %s
                    ORDER BY ps.scraped_at DESC
                """, (run_id,))
            else:
                cur.execute("""
                    SELECT ps.*, h.name as hotel_name, h.id as hotel_id_val
                    FROM price_snapshots ps
                    JOIN hotels h ON h.id = ps.hotel_id
                    WHERE ps.scraped_at > NOW() - INTERVAL '%s hours'
                    ORDER BY ps.scraped_at DESC
                """, (hours_back,))
            return [dict(r) for r in cur.fetchall()]


def get_recent_flight_snapshots(run_id: str = None, hours_back: int = 13) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if run_id:
                cur.execute("""
                    SELECT fs.*, t.name as trip_name
                    FROM flight_snapshots fs
                    JOIN trips t ON t.id = fs.trip_id
                    WHERE fs.run_id = %s
                """, (run_id,))
            else:
                cur.execute("""
                    SELECT fs.*, t.name as trip_name
                    FROM flight_snapshots fs
                    JOIN trips t ON t.id = fs.trip_id
                    WHERE fs.scraped_at > NOW() - INTERVAL '%s hours'
                    ORDER BY fs.scraped_at DESC
                """, (hours_back,))
            return [dict(r) for r in cur.fetchall()]


def get_current_low(hotel_id, provider, check_in, check_out) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM price_lows
                WHERE hotel_id = %s AND provider = %s
                  AND check_in = %s AND check_out = %s
            """, (hotel_id, provider, check_in, check_out))
            row = cur.fetchone()
            return dict(row) if row else None


def get_current_flight_low(trip_id, origin, destination, depart_date, return_date) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM flight_lows
                WHERE trip_id = %s AND origin = %s AND destination = %s
                  AND depart_date = %s AND return_date = %s
            """, (trip_id, origin, destination, depart_date, return_date))
            row = cur.fetchone()
            return dict(row) if row else None


# ── Alert detection ─────────────────────────────────────────────────────────────

def check_hotel_snapshot(snap: dict) -> list[dict]:
    """Returns list of alert dicts for a single snapshot."""
    alerts = []
    hotel_id  = str(snap["hotel_id"])
    provider  = snap["provider"]
    check_in  = snap["check_in"]
    check_out = snap["check_out"]
    price     = float(snap["price_total"])
    snap_id   = snap["id"]

    # Skip if already alerted recently for this hotel+dates
    if was_alerted_recently(hotel_id, check_in, check_out):
        return []

    # ── 1. Historic low ─────────────────────────────────────────────────────
    current_low = get_current_low(hotel_id, provider, check_in, check_out)
    if not current_low or price < float(current_low["low_price"]):
        prev_low = float(current_low["low_price"]) if current_low else None
        alerts.append({
            "hotel_name":  snap["hotel_name"],
            "hotel_id":    hotel_id,
            "check_in":    str(check_in),
            "check_out":   str(check_out),
            "provider":    provider,
            "price":       price,
            "prev_low":    prev_low,
            "alert_type":  "historic_low",
            "is_new_low":  True,
            "snap_id":     snap_id,
            "trip_id":     str(snap.get("trip_id", "")),
        })

    # ── 2. Fixed threshold breach ────────────────────────────────────────────
    thresholds = get_alert_thresholds(hotel_id=hotel_id)
    for t in thresholds:
        if t.get("threshold_price") and price <= float(t["threshold_price"]):
            # Only add if not already captured as a new low
            if not any(a["alert_type"] == "historic_low" for a in alerts):
                alerts.append({
                    "hotel_name": snap["hotel_name"],
                    "hotel_id":   hotel_id,
                    "check_in":   str(check_in),
                    "check_out":  str(check_out),
                    "provider":   provider,
                    "price":      price,
                    "prev_low":   float(t["threshold_price"]),
                    "alert_type": "threshold_breach",
                    "is_new_low": False,
                    "snap_id":    snap_id,
                    "trip_id":    str(snap.get("trip_id", "")),
                })

    # ── 3. Percentage drop vs rolling average ────────────────────────────────
    avg = get_rolling_avg(hotel_id, provider, check_in, check_out)
    if avg and avg > 0:
        pct_drop = ((avg - price) / avg) * 100
        if pct_drop >= PCT_DROP_THRESHOLD:
            if not any(a["alert_type"] in ("historic_low", "threshold_breach") for a in alerts):
                alerts.append({
                    "hotel_name":  snap["hotel_name"],
                    "hotel_id":    hotel_id,
                    "check_in":    str(check_in),
                    "check_out":   str(check_out),
                    "provider":    provider,
                    "price":       price,
                    "prev_low":    round(avg, 2),
                    "alert_type":  "pct_drop",
                    "is_new_low":  False,
                    "pct_drop":    round(pct_drop, 1),
                    "snap_id":     snap_id,
                    "trip_id":     str(snap.get("trip_id", "")),
                })

    return alerts


def check_flight_snapshot(snap: dict) -> list[dict]:
    alerts = []
    trip_id     = str(snap["trip_id"])
    origin      = snap["origin"]
    destination = snap["destination"]
    depart_date = snap["depart_date"]
    return_date = snap.get("return_date")
    price       = float(snap["price"])
    snap_id     = snap["id"]

    current_low = get_current_flight_low(trip_id, origin, destination, depart_date, return_date)
    if not current_low or price < float(current_low["low_price"]):
        prev_low = float(current_low["low_price"]) if current_low else None
        alerts.append({
            "trip_name":   snap.get("trip_name", ""),
            "trip_id":     trip_id,
            "origin":      origin,
            "destination": destination,
            "depart_date": str(depart_date),
            "return_date": str(return_date) if return_date else None,
            "airline":     snap.get("airline", "Unknown"),
            "price":       price,
            "prev_low":    prev_low,
            "alert_type":  "historic_low",
            "snap_id":     snap_id,
        })

    return alerts


# ── Main ────────────────────────────────────────────────────────────────────────

def run_alert_engine(run_id: str = None, dry_run: bool = False):
    logger.info(f"=== Alert Engine started at {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    hotel_snaps  = get_recent_hotel_snapshots(run_id=run_id)
    flight_snaps = get_recent_flight_snapshots(run_id=run_id)

    logger.info(f"Checking {len(hotel_snaps)} hotel snapshots + {len(flight_snaps)} flight snapshots")

    hotel_alerts  = []
    flight_alerts = []

    for snap in hotel_snaps:
        alerts = check_hotel_snapshot(snap)
        hotel_alerts.extend(alerts)

    for snap in flight_snaps:
        alerts = check_flight_snapshot(snap)
        flight_alerts.extend(alerts)

    total = len(hotel_alerts) + len(flight_alerts)
    logger.info(f"Found {len(hotel_alerts)} hotel alert(s) + {len(flight_alerts)} flight alert(s) = {total} total")

    if total == 0:
        logger.info("No alerts to send. All done.")
        return

    # ── Log alerts to DB ─────────────────────────────────────────────────────
    if not dry_run:
        for a in hotel_alerts:
            log_alert(
                hotel_id=a["hotel_id"],
                trip_id=a.get("trip_id"),
                snapshot_id=a["snap_id"],
                alert_type=a["alert_type"],
                price=a["price"],
                prev_low=a.get("prev_low"),
            )
        for a in flight_alerts:
            log_alert(
                hotel_id=None,
                trip_id=a.get("trip_id"),
                snapshot_id=a["snap_id"],
                alert_type=a["alert_type"],
                price=a["price"],
                prev_low=a.get("prev_low"),
            )

    # ── Print summary ────────────────────────────────────────────────────────
    if hotel_alerts:
        print("\n🏨 HOTEL ALERTS:")
        for a in hotel_alerts:
            tag = "🔻 NEW LOW" if a.get("is_new_low") else f"⚠️ {a['alert_type'].upper()}"
            prev = f"(was ${a['prev_low']:.0f})" if a.get("prev_low") else "(first record)"
            print(f"  {tag} | {a['hotel_name']} | {a['check_in']}→{a['check_out']} "
                  f"| {a['provider']} | ${a['price']:.0f} {prev}")

    if flight_alerts:
        print("\n✈️  FLIGHT ALERTS:")
        for a in flight_alerts:
            prev = f"(was ${a['prev_low']:.0f})" if a.get("prev_low") else "(first record)"
            print(f"  🔻 NEW LOW | {a['origin']}→{a['destination']} | "
                  f"Dep {a['depart_date']} Ret {a.get('return_date','—')} | "
                  f"{a['airline']} | ${a['price']:.0f} {prev}")

    # ── Send email digest ────────────────────────────────────────────────────
    if dry_run:
        logger.info("[DRY RUN] Skipping email send")
        return

    sent_any = False

    if hotel_alerts:
        subject = f"🏨 Travel Alert: {len(hotel_alerts)} hotel deal(s) found — {datetime.now().strftime('%b %d')}"
        html    = build_hotel_alert_html(hotel_alerts)
        ok = send_alert_email(subject, html)
        sent_any = sent_any or ok

    if flight_alerts:
        subject = f"✈️ Flight Alert: {len(flight_alerts)} deal(s) found — {datetime.now().strftime('%b %d')}"
        html    = build_flight_alert_html(flight_alerts)
        ok = send_alert_email(subject, html)
        sent_any = sent_any or ok

    if sent_any:
        logger.info("✅ Alert email(s) sent successfully")
    else:
        logger.error("❌ Failed to send alert email(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Travel tracker alert engine")
    parser.add_argument("--run-id", help="Check alerts for a specific scraper run ID")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts without saving or emailing")
    args = parser.parse_args()

    run_alert_engine(run_id=args.run_id, dry_run=args.dry_run)
