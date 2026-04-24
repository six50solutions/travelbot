# ✈️ Travel Tracker

Automated hotel + flight price tracker. Scrapes Google Hotels and Google Flights
twice daily via GitHub Actions, stores price history in Supabase, and sends
Microsoft Graph email alerts when new historic lows or threshold breaches are detected.

---

## Architecture

```
GitHub Actions (cron 7AM + 7PM CT)
       │
       ├── scrapers/hotel_scraper.py   → Playwright → Google Hotels
       ├── scrapers/flight_scraper.py  → Playwright → Google Flights
       │        ↓
       │   Supabase (Postgres)
       │   price_snapshots / flight_snapshots
       │   price_lows / flight_lows
       │
       └── alerts/alert_engine.py
               ↓
           Microsoft Graph → Email digest
```

---

## Setup

### 1. Clone and install locally

```bash
git clone https://github.com/YOUR_ORG/travel-tracker.git
cd travel-tracker
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Run schema in Supabase

Open your Supabase project → SQL Editor → paste and run `schema.sql`.

### 3. Configure your hotels and trips

Edit `config/hotels.json`. Add up to ~50 hotels and your trip windows.

```json
{
  "hotels": [
    {
      "name": "The Langham Chicago",
      "location": "Chicago, IL",
      "search_query": "The Langham Chicago",
      "tags": ["luxury", "downtown"]
    }
  ],
  "trips": [
    {
      "name": "Chicago Summer",
      "destination": "Chicago, IL",
      "origin": "ORD",
      "check_in_start": "2026-08-01",
      "check_in_end": "2026-08-31",
      "durations": [2, 3],
      "adults": 2
    }
  ]
}
```

Then seed to Supabase:

```bash
export SUPABASE_DB_URL="postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres"
python scripts/seed_from_config.py
```

### 4. Set up environment variables (local)

Create a `.env` file (never commit this):

```env
SUPABASE_DB_URL=postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
NOTIFY_FROM_EMAIL=adil@six50.io
NOTIFY_TO_EMAIL=adil@six50.io
```

These are the same Azure App Registration credentials used by `six50_ai_coo.py`.
Required Graph permissions (Application type, not Delegated):
- `Mail.Send`
- `Mail.Read`

### 5. Add GitHub Secrets

Go to your repo → Settings → Secrets and variables → Actions → New repository secret.

Add these 6 secrets:

| Secret Name | Value |
|---|---|
| `SUPABASE_DB_URL` | Your Supabase connection string |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | App registration client ID |
| `AZURE_CLIENT_SECRET` | App registration client secret |
| `NOTIFY_FROM_EMAIL` | Sending mailbox (e.g. adil@six50.io) |
| `NOTIFY_TO_EMAIL` | Alert recipient (can be same address) |

---

## First Run Checklist

```
[ ] schema.sql run in Supabase
[ ] config/hotels.json populated with your hotels + trips
[ ] python scripts/seed_from_config.py  ← run this once
[ ] .env file created locally
[ ] Dry run test:
      python scrapers/hotel_scraper.py --dry-run
      python scrapers/flight_scraper.py --dry-run
      python alerts/alert_engine.py --dry-run
[ ] All 6 GitHub Secrets added
[ ] Push to GitHub — Actions tab shows the workflow
[ ] Trigger manually: Actions → "Travel Tracker — Scrape & Alert" → Run workflow
```

---

## GitHub Actions Schedules

| Workflow | Schedule | What it does |
|---|---|---|
| `scraper.yml` | 7:00 AM CT + 7:00 PM CT | Hotels → Flights → Alerts (sequential) |
| `price_check.yml` | Manual only | On-demand check for specific hotel/dates |

---

## Bot: On-Demand Price Checks

Run locally or trigger via GitHub Actions → `price_check.yml` → Run workflow.

```bash
# Live scrape: check a specific hotel for dates
python bot/price_check.py --hotel "Langham" --check-in 2026-08-10 --nights 3

# Show all current historic lows in DB
python bot/price_check.py --lows

# Show cheapest upcoming dates for a hotel (from DB, no scraping)
python bot/price_check.py --hotel "Langham" --cheapest

# Show all prices for a trip
python bot/price_check.py --trip-id <uuid> --summary

# Set a price alert threshold ($250 or below → alert)
python bot/price_check.py --set-threshold --hotel "Langham" --price 250

# List all trips (to get UUIDs)
python bot/price_check.py --list-trips

# List all tracked hotels
python bot/price_check.py --list-hotels
```

---

## Alert Types

| Type | Trigger |
|---|---|
| `historic_low` | New all-time low for a hotel + date range + provider combo |
| `threshold_breach` | Price drops below your manually set threshold |
| `pct_drop` | Price is 10%+ below 30-day rolling average (configurable via `ALERT_PCT_DROP` env var) |

Deduplication: alerts won't re-fire for the same hotel + dates within a 12-hour window.

---

## Database Tables

| Table | Purpose |
|---|---|
| `hotels` | Your curated hotel list |
| `providers` | Whitelisted OTAs |
| `trips` | Trip configs (destination, date window, durations) |
| `trip_hotels` | Links hotels to trips |
| `price_snapshots` | Every scraped price |
| `price_lows` | Current historic low per hotel/dates/provider |
| `flight_snapshots` | Every scraped flight price |
| `flight_lows` | Current historic low per route/dates |
| `alert_thresholds` | Your manual price targets |
| `alert_log` | Alert fire history (dedup + audit) |

---

## Troubleshooting

**Scrapers return no prices**
Google's DOM changes frequently. Check the GitHub Actions log for the actual HTML.
The selectors in `hotel_scraper.py` and `flight_scraper.py` may need updating.
Run with `--dry-run` locally first to debug.

**MSAL token errors**
Confirm Azure App Registration has `Mail.Send` set as **Application** permission (not Delegated),
and admin consent has been granted. Same setup as `six50_ai_coo.py`.

**Supabase connection timeout**
Use the direct connection string (port 5432), not the connection pooler, for long-running scripts.

**GitHub Actions timeout**
50 hotels × 30 date combos = 1,500 searches. At 5 sec/search that's ~2 hours.
Reduce the date window in your trip config, or split trips across multiple workflows.
