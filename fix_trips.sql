-- Fix trip date windows to match hotels.json (14-day windows)
-- Run in Supabase SQL Editor

UPDATE trips SET
    check_in_start = '2026-07-01',
    check_in_end   = '2026-07-14',
    durations      = '{7}'
WHERE name = 'Hawaii — Maui';

UPDATE trips SET
    check_in_start = '2026-07-01',
    check_in_end   = '2026-07-14',
    durations      = '{7}'
WHERE name = 'Hawaii — Oahu';

UPDATE trips SET
    check_in_start = '2026-06-01',
    check_in_end   = '2026-06-14',
    durations      = '{3}'
WHERE name = 'Miami';

UPDATE trips SET
    check_in_start = '2026-07-01',
    check_in_end   = '2026-07-14',
    durations      = '{7}'
WHERE name = 'Cancun';

UPDATE trips SET
    check_in_start = '2026-09-01',
    check_in_end   = '2026-09-14',
    durations      = '{5}'
WHERE name = 'Europe — Paris';

UPDATE trips SET
    check_in_start = '2026-09-01',
    check_in_end   = '2026-09-14',
    durations      = '{5}'
WHERE name = 'Europe — London';

UPDATE trips SET
    check_in_start = '2026-09-15',
    check_in_end   = '2026-09-28',
    durations      = '{5}'
WHERE name = 'Europe — Barcelona + Rome';

UPDATE trips SET
    check_in_start = '2026-10-01',
    check_in_end   = '2026-10-14',
    durations      = '{7}'
WHERE name = 'Asia — Tokyo';

UPDATE trips SET
    check_in_start = '2026-10-15',
    check_in_end   = '2026-10-28',
    durations      = '{5}'
WHERE name = 'Asia — Singapore + Bangkok';

UPDATE trips SET
    check_in_start = '2026-11-01',
    check_in_end   = '2026-11-14',
    durations      = '{7}'
WHERE name = 'Bali';

UPDATE trips SET
    check_in_start = '2026-11-15',
    check_in_end   = '2026-11-28',
    durations      = '{5}'
WHERE name = 'Dubai';

-- Verify
SELECT name, check_in_start, check_in_end, durations FROM trips ORDER BY name;
