-- ============================================================
-- TRAVEL TRACKER SCHEMA
-- Run this in your Supabase SQL editor
-- ============================================================

-- Curated hotel list (~50 properties)
CREATE TABLE IF NOT EXISTS hotels (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    google_hotel_id TEXT,                       -- Google Hotels property ID (from URL)
    location        TEXT NOT NULL,              -- City/region for search (e.g. "Chicago, IL")
    search_query    TEXT NOT NULL,              -- Exact search string used in Google Hotels
    tags            TEXT[] DEFAULT '{}',        -- e.g. ['beach','business','family']
    active          BOOLEAN DEFAULT TRUE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Whitelisted providers (OTAs + direct)
CREATE TABLE IF NOT EXISTS providers (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,           -- e.g. "Booking.com", "Hotels.com", "Direct"
    active      BOOLEAN DEFAULT TRUE
);

-- Trip configurations (date range + duration combos to track)
CREATE TABLE IF NOT EXISTS trips (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,              -- e.g. "Summer Chicago 3-nights"
    origin          TEXT,                       -- For flight tracking (IATA code)
    destination     TEXT NOT NULL,              -- City or IATA code
    check_in_start  DATE NOT NULL,              -- Start of check-in window
    check_in_end    DATE NOT NULL,              -- End of check-in window
    durations       INT[] NOT NULL,             -- Stay lengths to check, e.g. [2, 3, 5]
    adults          INT DEFAULT 2,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Link hotels to trips (which hotels to watch for each trip)
CREATE TABLE IF NOT EXISTS trip_hotels (
    trip_id     UUID REFERENCES trips(id) ON DELETE CASCADE,
    hotel_id    UUID REFERENCES hotels(id) ON DELETE CASCADE,
    PRIMARY KEY (trip_id, hotel_id)
);

-- Raw price snapshots from every scrape run
CREATE TABLE IF NOT EXISTS price_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    hotel_id        UUID REFERENCES hotels(id) ON DELETE CASCADE,
    trip_id         UUID REFERENCES trips(id) ON DELETE SET NULL,
    provider        TEXT NOT NULL,
    check_in        DATE NOT NULL,
    check_out       DATE NOT NULL,
    price_total     NUMERIC(10, 2) NOT NULL,    -- Total stay price
    price_per_night NUMERIC(10, 2),             -- Computed: price_total / nights
    currency        TEXT DEFAULT 'USD',
    room_type       TEXT,
    cancellable     BOOLEAN,
    scraped_at      TIMESTAMPTZ DEFAULT NOW(),
    run_id          TEXT                        -- Links snapshots from same scrape run
);

-- Derived: historic low per hotel+checkin+checkout+provider combo
CREATE TABLE IF NOT EXISTS price_lows (
    id              BIGSERIAL PRIMARY KEY,
    hotel_id        UUID REFERENCES hotels(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    check_in        DATE NOT NULL,
    check_out       DATE NOT NULL,
    low_price       NUMERIC(10, 2) NOT NULL,
    snapshot_id     BIGINT REFERENCES price_snapshots(id),
    recorded_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (hotel_id, provider, check_in, check_out)
);

-- Per-hotel or per-trip alert thresholds
CREATE TABLE IF NOT EXISTS alert_thresholds (
    id              SERIAL PRIMARY KEY,
    hotel_id        UUID REFERENCES hotels(id) ON DELETE CASCADE,
    trip_id         UUID REFERENCES trips(id) ON DELETE CASCADE,
    threshold_price NUMERIC(10, 2),             -- Alert if price drops below this
    pct_below_avg   NUMERIC(5, 2),              -- Alert if price is X% below rolling avg
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Alert fire log (prevents duplicate notifications)
CREATE TABLE IF NOT EXISTS alert_log (
    id              BIGSERIAL PRIMARY KEY,
    hotel_id        UUID REFERENCES hotels(id),
    trip_id         UUID REFERENCES trips(id),
    snapshot_id     BIGINT REFERENCES price_snapshots(id),
    alert_type      TEXT NOT NULL,              -- 'historic_low' | 'threshold_breach' | 'pct_drop'
    price           NUMERIC(10, 2),
    prev_low        NUMERIC(10, 2),
    notified_at     TIMESTAMPTZ DEFAULT NOW(),
    notification_sent BOOLEAN DEFAULT FALSE
);

-- Flight price snapshots
CREATE TABLE IF NOT EXISTS flight_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    trip_id         UUID REFERENCES trips(id) ON DELETE CASCADE,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    depart_date     DATE NOT NULL,
    return_date     DATE,
    price           NUMERIC(10, 2) NOT NULL,
    airline         TEXT,
    stops           INT DEFAULT 0,
    duration_mins   INT,
    provider        TEXT,
    scraped_at      TIMESTAMPTZ DEFAULT NOW(),
    run_id          TEXT
);

-- Flight historic lows
CREATE TABLE IF NOT EXISTS flight_lows (
    id              BIGSERIAL PRIMARY KEY,
    trip_id         UUID REFERENCES trips(id) ON DELETE CASCADE,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    depart_date     DATE NOT NULL,
    return_date     DATE,
    low_price       NUMERIC(10, 2) NOT NULL,
    snapshot_id     BIGINT REFERENCES flight_snapshots(id),
    recorded_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (trip_id, origin, destination, depart_date, return_date)
);

-- ============================================================
-- INDEXES
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_snapshots_hotel_dates ON price_snapshots(hotel_id, check_in, check_out);
CREATE INDEX IF NOT EXISTS idx_snapshots_scraped_at  ON price_snapshots(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_lows_hotel            ON price_lows(hotel_id, check_in);
CREATE INDEX IF NOT EXISTS idx_alert_log_hotel       ON alert_log(hotel_id, notified_at DESC);
CREATE INDEX IF NOT EXISTS idx_flight_snaps_trip     ON flight_snapshots(trip_id, depart_date);

-- ============================================================
-- SEED: DEFAULT PROVIDERS
-- ============================================================
INSERT INTO providers (name) VALUES
    ('Booking.com'),
    ('Hotels.com'),
    ('Expedia'),
    ('Direct'),
    ('Hilton'),
    ('Marriott'),
    ('Hyatt'),
    ('IHG')
ON CONFLICT (name) DO NOTHING;
