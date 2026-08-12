"""
10_find_flow_replacements.py

Replaces the 7 river_flow stations that turned out to have no `flow`
measure (only `level`/stage) with real alternatives, and writes an
updated backtest_station_selection.csv (47 originally-working rows +
7 new replacements = 54 total), ready to feed straight back into
09_pull_historical_readings.py.

Reuses station_history_cache.csv from Step 1 (08_select_backtest_stations.py)
for dateOpened, rather than re-hitting the API for that — only makes new
API calls to verify each replacement candidate actually has a real daily
flow measure, which is the thing that failed silently last time.

Run from inside python/, after 08_select_backtest_stations.py has already
produced station_history_cache.csv.
"""

import csv
import os
import time

import psycopg2
import requests

HYDROLOGY_API_BASE = "https://environment.data.gov.uk/hydrology"
ORIGINAL_CSV = "backtest_station_selection.csv"
CACHE_PATH = "station_history_cache.csv"
OUTPUT_CSV = "backtest_station_selection_v2.csv"
MIN_HISTORY_CUTOFF = "2015-01-01"

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

# area -> list of station_ids that need replacing (from the 8-row zero-count
# result — 7 river_flow gaps; SETTLE CREAMERY's groundwater fix is separate,
# handled directly in 09_pull_historical_readings.py's matcher, not here)
FLOWLESS_STATION_IDS = {
    # Lincolnshire and Northamptonshire / West Drain
    "36bffa71-7516-4c7c-a607-8fd11e23ffa6",
    "803901e5-bd85-4ce8-a772-d1c288ffb310",  # Solent and South Downs / Eversley
    "f4cf8e7b-b38a-4978-8f7d-24dcc6bf0cb6",  # Wessex / Simonsbath
    "64102eca-26e9-44b5-8e77-9d398e714b49",  # Wessex / Monsoon Drain Bridge
    "dd2cb948-d1e6-4130-9c57-4bcc8547ccdb",  # West Midlands / Leek
    "5070e5a8-5e26-48fa-b029-7d2edf908a30",  # Yorkshire / Skeffling
    "7b0e021a-060d-498d-a817-c58083537388",  # Yorkshire / Low Bentham
}


def load_history_cache():
    cache = {}
    with open(CACHE_PATH, newline="") as f:
        for row in csv.DictReader(f):
            cache[row["station_id"]] = row["date_opened"] or None
    return cache


def has_real_flow_measure(station_id):
    """Live check: does this station have an actual daily flow measure?"""
    url = f"{HYDROLOGY_API_BASE}/id/stations/{station_id}/measures.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    measures = resp.json().get("items", [])
    for m in measures:
        if m.get("parameter", "").lower() == "flow" and m.get("period") == 86400:
            return True
    return False


def main():
    print("Connecting to RDS...", flush=True)
    conn = psycopg2.connect(**DB_CONFIG)
    print("Connected.", flush=True)

    history_cache = load_history_cache()
    print(
        f"Loaded {len(history_cache)} cached dateOpened value(s).", flush=True)

    # Load the original selection, split into keepers and stations to drop.
    with open(ORIGINAL_CSV, newline="") as f:
        original_rows = list(csv.DictReader(f))

    keepers = [r for r in original_rows if r["station_id"]
               not in FLOWLESS_STATION_IDS]
    already_selected_ids = {r["station_id"] for r in original_rows}

    # Which areas need how many replacements
    needed = {}
    for r in original_rows:
        if r["station_id"] in FLOWLESS_STATION_IDS:
            needed[r["area_name"]] = needed.get(r["area_name"], 0) + 1

    print(f"Need replacements: {needed}", flush=True)

    replacements = []

    with conn.cursor() as cur:
        for area, n_needed in needed.items():
            print(
                f"\n=== {area}: need {n_needed} replacement(s) ===", flush=True)
            cur.execute(
                """
                SELECT s.station_id, s.station_name, s.lat, s.long
                FROM stations s
                JOIN areas a ON s.area_code = a.area_code
                WHERE a.area_name = %s AND s.station_type = 'river_flow' AND s.source_api = 'hydrology'
                ORDER BY s.station_id;
                """,
                (area,),
            )
            candidates = cur.fetchall()
            found = 0

            for station_id, name, lat, lon in candidates:
                if found >= n_needed:
                    break
                if station_id in already_selected_ids:
                    continue  # already selected (working or flowless) — skip

                date_opened = history_cache.get(station_id)
                if not date_opened or date_opened > MIN_HISTORY_CUTOFF:
                    # not in cache (wasn't a candidate before) or too recent
                    continue

                time.sleep(0.2)
                if not has_real_flow_measure(station_id):
                    print(
                        f"  skip {name} ({station_id}): no daily flow measure", flush=True)
                    continue

                print(
                    f"  SELECTED: {name} ({station_id}), opened {date_opened}", flush=True)
                replacements.append({
                    "area_name": area,
                    "station_type": "river_flow",
                    "station_id": station_id,
                    "name": name,
                    "lat": lat,
                    "long": lon,
                    "source_api": "hydrology",
                    "date_opened": date_opened,
                })
                found += 1

            if found < n_needed:
                print(f"  WARNING: only found {found}/{n_needed} replacement(s) for {area}. "
                      f"May need to widen the search (e.g. relax MIN_HISTORY_CUTOFF).", flush=True)

    conn.close()

    all_rows = keepers + replacements
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "area_name", "station_type", "station_id", "name",
            "lat", "long", "source_api", "date_opened",
        ])
        writer.writeheader()
        for r in all_rows:
            writer.writerow(r)

    print(f"\nWritten {len(all_rows)} stations to {OUTPUT_CSV} "
          f"({len(keepers)} kept + {len(replacements)} replaced).")


if __name__ == "__main__":
    main()
