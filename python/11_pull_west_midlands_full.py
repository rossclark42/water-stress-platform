"""
11_pull_west_midlands_full.py

Full-depth pull: every hydrology-sourced station in West Midlands (780
stations: 403 groundwater, 77 rainfall, 300 river_flow), not a curated
subset. This is the MVP's main proof-of-concept dataset — dense enough
coverage in one area that multiple demo site locations (e.g. 5 breweries)
get genuinely different nearest-station assignments.

Shares all the same measure-selection logic as 09_pull_historical_readings.py
(flow-preferred/river_level-fallback for river_flow, dipped-preferred/
logged-fallback for groundwater, daily-total for rainfall) — that logic
matters even more here, since at 300 river_flow stations we'd expect
roughly 40% (~120) to be level-only based on the earlier sample.

Run 02_add_river_level_support.sql before this (adds the river_level
parameter and stations.flow_measure_type — this script depends on both).

Run from inside python/.
Requires: psycopg2, requests
"""

import csv
import time
from datetime import date

import psycopg2
import psycopg2.extras
import requests


def request_with_retry(url, params=None, timeout=30, max_retries=6):
    """GET with exponential backoff on HTTP 429 — see 09_pull_historical_readings.py
    for the full rationale (confirmed live: the EA API rate-limits after
    sustained volume; this matters far more here at 780 stations than it
    did at 210)."""
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


HYDROLOGY_API_BASE = "https://environment.data.gov.uk/hydrology"
AREA_NAME = "West Midlands"
MIN_DATE = "2015-01-01"
MAX_DATE = date.today().isoformat()

DB_CONFIG = {
    "host": "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "waterstress",
    "user": "postgres",
    "password": "REDACTED",  # fill in before running
    "connect_timeout": 10,
}

# True: only process the first 5 stations, to confirm everything works
# before committing to the full ~780-station / ~20-30 min run.
TEST_MODE = False


def get_all_area_stations(conn):
    query = """
        SELECT s.station_id, s.station_name, s.station_type
        FROM stations s
        JOIN areas a ON s.area_code = a.area_code
        WHERE a.area_name = %s AND s.source_api = 'hydrology'
        ORDER BY s.station_type, s.station_id;
    """
    with conn.cursor() as cur:
        cur.execute(query, (AREA_NAME,))
        return cur.fetchall()


def get_measures(station_id):
    url = f"{HYDROLOGY_API_BASE}/id/stations/{station_id}/measures.json"
    resp = request_with_retry(url, timeout=15)
    return resp.json().get("items", [])


def pick_measure(measures, station_type):
    """Identical logic to 09_pull_historical_readings.py — see that file
    for full rationale comments."""
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

    # Logged groundwater: reduce to closest-to-9am reading per day.
    # (Yearly chunking removed — confirmed it doesn't rescue anything; a
    # wide-range 0 means genuinely no data, not a range-size API limit.
    # Removing it also means fewer wasted API calls at 780-station scale.)
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

    stations = get_all_area_stations(conn)
    print(
        f"Found {len(stations)} hydrology-sourced stations in {AREA_NAME}.", flush=True)

    if TEST_MODE:
        stations = stations[:5]
        print(
            f"TEST_MODE: processing only {len(stations)} station(s).", flush=True)

    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT station_id FROM readings")
        already_loaded = {row[0] for row in cur.fetchall()}
    print(f"{len(already_loaded)} station(s) already have data loaded — will be skipped.", flush=True)

    total_rows_loaded = 0
    skipped_no_measure = 0
    river_level_count = 0
    # matched measure, but 0 readings returned — logged, not treated as an error
    no_data_stations = []

    with conn.cursor() as cur:
        for i, (station_id, name, station_type) in enumerate(stations, 1):
            if station_id in already_loaded:
                print(
                    f"[{i}/{len(stations)}] {name} ({station_id}) — already loaded, skipping.", flush=True)
                continue

            print(
                f"\n[{i}/{len(stations)}] {station_type} / {name} ({station_id})", flush=True)

            measures = get_measures(station_id)
            measure_id, needs_time_filter, parameter_used = pick_measure(
                measures, station_type)

            if not measure_id:
                print(
                    f"  SKIP: no matching measure among {len(measures)} available.", flush=True)
                skipped_no_measure += 1
                time.sleep(0.2)
                continue

            print(
                f"  Using measure: {measure_id} (parameter={parameter_used})", flush=True)
            readings = fetch_readings(measure_id, needs_time_filter)

            rows = [
                (station_id, r.get("dateTime") or r.get("date"), parameter_used,
                 r.get("value"), r.get("quality"), "hydrology_api")
                for r in readings if (r.get("dateTime") or r.get("date"))
            ]

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
                if station_type == "river_flow":
                    cur.execute(
                        "UPDATE stations SET flow_measure_type = %s WHERE station_id = %s",
                        (parameter_used, station_id),
                    )
                    if parameter_used == "river_level":
                        river_level_count += 1
                conn.commit()
                total_rows_loaded += len(rows)
                print(f"  Loaded {len(rows)} row(s).", flush=True)
            else:
                # A real measure was matched, but it genuinely has no data
                # in our window — same category as SETTLE CREAMERY: a
                # station whose metadata looks fine but has gone quiet.
                # Expected at this scale; logged for the record, not
                # something to keep retrying.
                print(
                    f"  NO DATA: matched measure but 0 readings in {MIN_DATE}-{MAX_DATE}.", flush=True)
                no_data_stations.append((station_id, station_type, name))

            time.sleep(0.3)

            if i % 40 == 0:
                print(
                    f"  --- pausing 10s after {i} stations, to stay well under the rate limit ---", flush=True)
                time.sleep(10)

            if i % 50 == 0:
                print(f"\n--- progress: {i}/{len(stations)}, {total_rows_loaded} rows loaded so far, "
                      f"{skipped_no_measure} skipped, {len(no_data_stations)} no-data, "
                      f"{river_level_count} river_level fallbacks ---", flush=True)

    conn.close()

    if no_data_stations:
        with open("west_midlands_no_data_stations.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["station_id", "station_type", "name"])
            writer.writerows(no_data_stations)
        print(f"\n{len(no_data_stations)} station(s) with a matched measure but no readings in "
              f"the window — written to west_midlands_no_data_stations.csv for the record.")

    print(f"\nDone. {total_rows_loaded} total reading(s) loaded across {len(stations)} station(s). "
          f"{skipped_no_measure} skipped (no measure at all), "
          f"{len(no_data_stations)} matched but had no data in window, "
          f"{river_level_count} river_flow stations used the river_level fallback.")


if __name__ == "__main__":
    main()
