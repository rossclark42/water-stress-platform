"""
09_pull_historical_readings.py

Phase 2, Step 2: pull multi-year historical readings for the ~54 stations
selected in backtest_station_selection.csv, via the EA Hydrology API, and
load them into RDS's `readings` table.

Run 01_create_readings_table.sql first (once) — this script assumes the
table already exists.

For each station, this script:
  1. Fetches the station's list of `measures` (time series) from the API
  2. Picks the single best-matching measure for that station's parameter
     type (river_flow -> daily mean flow, rainfall -> daily total rainfall,
     groundwater -> daily-ish groundwater level, preferring "Dipped" over
     "Logged" — see NOTE below)
  3. Pulls readings for that measure from MIN_DATE to today
  4. Loads them into `readings`, idempotently (ON CONFLICT DO NOTHING)

NOTE ON GROUNDWATER — verify live before trusting at scale:
Per the EA Hydrology API docs, groundwater comes in two flavours:
  - "Groundwater Dipped": periodic manual readings (already roughly daily
    or less frequent) — used as-is if available.
  - "Groundwater Logged": continuous sub-daily sensor data — the docs'
    own recommended trick is to filter to a fixed time (09:00:00) to get
    one approximate daily value per day, rather than pulling every 15-min
    point (which would blow past reasonable row counts for no benefit
    here). This script does that automatically when only a Logged series
    is available, but the matching logic (checking `qualifier` for the
    substring "Groundwater") is inferred from the docs, not confirmed
    against a live response — check the printed measure list for the
    first groundwater station before trusting the rest of the run.

NOTE ON MATCHING LOGIC GENERALLY: field value casing (e.g. whether
`periodName` comes back as "Daily" or "daily") isn't fully pinned down by
the docs, so matching below is case-insensitive. If a station comes back
with "no matching measure found", print its raw /measures response and
adjust the matcher — don't assume the station simply lacks the data.

Run from inside python/, per project convention.
Requires: psycopg2, requests
"""

import csv
import os
import time
from datetime import date

import psycopg2
import psycopg2.extras
import requests


def request_with_retry(url, params=None, timeout=30, max_retries=6):
    """
    GET with exponential backoff on HTTP 429 (confirmed live: the EA API
    rate-limits after sustained volume — the 210-station national run hit
    this at station 177/210 and crashed uncaught). Respects Retry-After if
    the API sends one, otherwise backs off 5s/10s/20s/40s/60s/60s.
    """
    delay = 5
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = int(
                retry_after) if retry_after and retry_after.isdigit() else delay
            print(
                f"    Rate limited (429) — waiting {wait}s (retry {attempt + 1}/{max_retries})...", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(
        f"Gave up after {max_retries} retries due to repeated 429s: {url}")


# --- config ---
HYDROLOGY_API_BASE = "https://environment.data.gov.uk/hydrology"
STATION_LIST_CSV = "backtest_station_selection_v4_national.csv"
MIN_DATE = "2015-01-01"
MAX_DATE = date.today().isoformat()
# per EA docs' own recommended approach for logged data
GROUNDWATER_DIP_TIME = "09:00:00"

PGPASSWORD = os.environ.get("PGPASSWORD")
if not PGPASSWORD:
    raise SystemExit(
        "Set PGPASSWORD environment variable first (do not hardcode it here).")

DB_CONFIG = {
    "host": "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "waterstress",
    "user": "postgres",
    "password": PGPASSWORD,
    "connect_timeout": 10,
}

# True: only process the first 2 stations end-to-end (measure selection +
# readings pull + DB load) to confirm everything works before the full run.
TEST_MODE = False


def load_selected_stations():
    stations = []
    with open(STATION_LIST_CSV, newline="") as f:
        for row in csv.DictReader(f):
            stations.append(row)
    return stations


def get_measures(station_id):
    url = f"{HYDROLOGY_API_BASE}/id/stations/{station_id}/measures.json"
    resp = request_with_retry(url, timeout=15)
    return resp.json().get("items", [])


def pick_measure(measures, station_type):
    """
    Return (measure_id, needs_time_filter, parameter_used) for the best-
    matching measure, or (None, False, None) if nothing suitable exists.

    river_flow prefers a real `flow` measure; if none exists (confirmed at
    national scale to be common — ~40% of a sample were level-only, often
    tidal-influenced sites where flow can't be reliably computed), falls
    back to `level` (river stage) instead of dropping the station. This is
    the river_level fallback agreed during Phase 2 — every parameter is
    normalized to its own station's historical percentile downstream
    (Phase 3), which is what makes flow and river_level legitimately
    comparable as drought signals despite being different physical units.
    """
    def notation_of(m):
        return m.get("notation") or m.get("@id", "").rstrip("/").split("/")[-1]

    if station_type == "river_flow":
        for m in measures:
            if (m.get("parameter", "").lower() == "flow"
                    and str(m.get("periodName", "")).lower() == "daily"
                    and str(m.get("valueType", "")).lower() == "mean"):
                return notation_of(m), False, "flow"
        for m in measures:
            if m.get("parameter", "").lower() == "flow" and m.get("period") == 86400:
                return notation_of(m), False, "flow"

        # No flow measure at all — fall back to level. Prefer `min` (the
        # more drought-relevant of the two available daily stats) over
        # `max`, since a station's daily min level is the more direct
        # analogue to low-flow drought signal.
        for value_type in ("min", "max"):
            for m in measures:
                if (m.get("parameter", "").lower() == "level"
                        and str(m.get("periodName", "")).lower() == "daily"
                        and str(m.get("valueType", "")).lower() == value_type):
                    return notation_of(m), False, "river_level"

    elif station_type == "rainfall":
        for m in measures:
            if (m.get("parameter", "").lower() == "rainfall"
                    and str(m.get("periodName", "")).lower() == "daily"
                    and str(m.get("valueType", "")).lower() == "total"):
                return notation_of(m), False, "rainfall"
        for m in measures:
            if m.get("parameter", "").lower() == "rainfall" and m.get("period") == 86400:
                return notation_of(m), False, "rainfall"

    elif station_type == "groundwater":
        dipped = [m for m in measures if "dipped" in str(
            m.get("qualifier", "")).lower()]
        if dipped:
            return notation_of(dipped[0]), False, "groundwater_level"
        logged = [m for m in measures if "logged" in str(
            m.get("qualifier", "")).lower()]
        if logged:
            return notation_of(logged[0]), True, "groundwater_level"

    return None, False, None


def fetch_readings(measure_id, needs_time_filter):
    url = f"{HYDROLOGY_API_BASE}/id/measures/{measure_id}/readings.json"
    params = {"mineq-date": MIN_DATE, "maxeq-date": MAX_DATE, "_limit": 100000}

    if not needs_time_filter:
        resp = request_with_retry(url, params=params, timeout=30)
        return resp.json().get("items", [])

    # Logged groundwater: reduce to the closest-to-9am reading per day.
    # NOTE: an earlier version of this function chunked into yearly
    # requests on the theory that wide date ranges silently failed. That
    # theory was wrong — confirmed live that a wide-range 0 and a
    # yearly-chunk 0 mean the same thing (genuinely no data in this
    # window), not a range-size API limit. Chunking was pure wasted API
    # calls; removed rather than kept "just in case".
    from datetime import datetime

    resp = request_with_retry(url, params=params, timeout=30)
    items = resp.json().get("items", [])

    by_day = {}
    for r in items:
        dt_str = r.get("dateTime")
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        day = dt.date()
        distance = abs((dt.hour * 60 + dt.minute) - 9 * 60)
        if day not in by_day or distance < by_day[day][0]:
            by_day[day] = (distance, r)
    return [r for _, r in by_day.values()]


def main():
    print("Connecting to RDS...", flush=True)
    conn = psycopg2.connect(**DB_CONFIG)
    print("Connected.", flush=True)

    stations = load_selected_stations()
    if TEST_MODE:
        stations = stations[:2]
        print(
            f"TEST_MODE: processing only {len(stations)} station(s).", flush=True)

    # Skip stations that already have rows loaded — makes resuming after a
    # crash (e.g. the 429 that killed the run at 177/210) cheap: no wasted
    # API calls re-fetching stations that already succeeded. Stations that
    # genuinely returned 0 readings last time (no rows written) are NOT
    # skipped, so they get a fair retry.
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT station_id FROM readings")
        already_loaded = {row[0] for row in cur.fetchall()}
    print(f"{len(already_loaded)} station(s) already have data loaded — will be skipped.", flush=True)

    total_rows_loaded = 0

    with conn.cursor() as cur:
        for i, s in enumerate(stations, 1):
            station_id = s["station_id"]
            station_type = s["station_type"]

            if station_id in already_loaded:
                print(
                    f"[{i}/{len(stations)}] {s['name']} ({station_id}) — already loaded, skipping.", flush=True)
                continue

            print(
                f"\n[{i}/{len(stations)}] {s['area_name']} / {station_type} / {s['name']} ({station_id})", flush=True)

            measures = get_measures(station_id)
            measure_id, needs_time_filter, parameter_used = pick_measure(
                measures, station_type)

            if not measure_id:
                print(f"  WARNING: no matching measure found among {len(measures)} available. "
                      f"Raw measures: {measures}", flush=True)
                continue

            print(f"  Using measure: {measure_id} (parameter={parameter_used})"
                  f"{' (9am time filter for logged groundwater)' if needs_time_filter else ''}", flush=True)

            readings = fetch_readings(measure_id, needs_time_filter)
            print(
                f"  Fetched {len(readings)} reading(s) from {MIN_DATE} to {MAX_DATE}.", flush=True)

            rows = []
            for r in readings:
                dt = r.get("dateTime") or r.get("date")
                if not dt:
                    continue
                rows.append((
                    station_id,
                    dt,
                    parameter_used,
                    r.get("value"),  # None/missing when quality == 'Missing'
                    r.get("quality"),
                    "hydrology_api",
                ))

            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO readings (station_id, reading_datetime, parameter, value, quality_flag, source)
                    VALUES %s
                    ON CONFLICT (station_id, reading_datetime, parameter) DO NOTHING
                    """,
                    rows,
                )
                # Record which kind of measure this river_flow station actually
                # provides, so Phase 5's site lookup is a single fast lookup on
                # `stations` rather than a scan of `readings`.
                if station_type == "river_flow":
                    cur.execute(
                        "UPDATE stations SET flow_measure_type = %s WHERE station_id = %s",
                        (parameter_used, station_id),
                    )
                conn.commit()
                total_rows_loaded += len(rows)
                print(
                    f"  Loaded {len(rows)} row(s) into readings.", flush=True)

            # be polite to the free API — this endpoint returns more data per call
            time.sleep(0.3)

            if i % 40 == 0:
                print(
                    f"  --- pausing 10s after {i} stations, to stay well under the rate limit ---", flush=True)
                time.sleep(10)

    conn.close()
    print(
        f"\nDone. {total_rows_loaded} total reading(s) loaded across {len(stations)} station(s).", flush=True)


if __name__ == "__main__":
    main()
