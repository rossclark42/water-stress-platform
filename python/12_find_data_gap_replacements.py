"""
12_find_data_gap_replacements.py

Finds replacements for the 5 stations in backtest_station_selection_v3_national.csv
that have zero readings in 2015-2026 despite passing the dateOpened check —
proof that dateOpened alone isn't sufficient (a station can have opened
decades ago and still have gone quiet since). This time, candidates are
verified with an actual live data check — a small real readings fetch,
not just metadata — before being accepted.

Run from inside python/, after 08's national run and station_history_cache.csv
both exist.
"""

import csv
import time

import psycopg2
import requests

HYDROLOGY_API_BASE = "https://environment.data.gov.uk/hydrology"
ORIGINAL_CSV = "backtest_station_selection_v3_national.csv"
CACHE_PATH = "station_history_cache.csv"
OUTPUT_CSV = "backtest_station_selection_v4_national.csv"
MIN_DATE = "2015-01-01"
MAX_DATE = "2026-08-10"
MIN_HISTORY_CUTOFF = "2015-01-01"

DB_CONFIG = {
    "host": "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "waterstress",
    "user": "postgres",
    "password": None,  # fill in before running
    "connect_timeout": 10,
}

# (area, station_type, station_id) for the 5 confirmed-empty stations
GAP_STATIONS = {
    ("Cumbria and Lancashire", "groundwater",
     "f9c59889-1eac-4c24-a0dd-0476924f9cde"),   # SILECROFT OBH
    ("Cumbria and Lancashire", "groundwater",
     "3c725b8d-6eb0-4266-87f7-971786d21be9"),   # NESTLES NO. 1 CAPPED
    ("Cumbria and Lancashire", "groundwater",
     "a5ec3f6c-f3d9-40ff-bf03-0ae9364110d5"),   # RED SCAR MILL COLNE
    ("Greater Manchester Merseyside and Cheshire", "groundwater",
     "efbd9f68-1c6f-4abe-8c4a-778e3af97be7"),  # COAST ROAD WEST
    # SETTLE CREAMERY
    ("Yorkshire", "groundwater", "6a400fe3-2c51-451f-a7a3-62bf82d6de93"),
}


def notation_of(m):
    return m.get("notation") or m.get("@id", "").rstrip("/").split("/")[-1]


def pick_measure(measures, station_type):
    """Same logic as 09/11 — see those files for full rationale."""
    if station_type == "groundwater":
        dipped = [m for m in measures if "dipped" in str(
            m.get("qualifier", "")).lower()]
        if dipped:
            return notation_of(dipped[0]), False
        logged = [m for m in measures if "logged" in str(
            m.get("qualifier", "")).lower()]
        if logged:
            return notation_of(logged[0]), True
    elif station_type == "rainfall":
        for m in measures:
            if (m.get("parameter", "").lower() == "rainfall"
                    and str(m.get("periodName", "")).lower() == "daily"):
                return notation_of(m), False
    elif station_type == "river_flow":
        for m in measures:
            if m.get("parameter", "").lower() == "flow" and m.get("period") == 86400:
                return notation_of(m), False
        for value_type in ("min", "max"):
            for m in measures:
                if (m.get("parameter", "").lower() == "level"
                        and str(m.get("periodName", "")).lower() == "daily"
                        and str(m.get("valueType", "")).lower() == value_type):
                    return notation_of(m), False
    return None, False


def has_real_data_in_window(station_id):
    """The actual fix: verify a small real sample exists in 2015-2026,
    not just that the station opened before 2015. This is the check that
    was missing before — dateOpened told us the station EXISTED that
    early, not that it's still producing data now."""
    measures_resp = requests.get(
        f"{HYDROLOGY_API_BASE}/id/stations/{station_id}/measures.json", timeout=15)
    measures_resp.raise_for_status()
    measures = measures_resp.json().get("items", [])

    for station_type in ("groundwater", "rainfall", "river_flow"):
        measure_id, _ = pick_measure(measures, station_type)
        if measure_id:
            resp = requests.get(
                f"{HYDROLOGY_API_BASE}/id/measures/{measure_id}/readings.json",
                params={"mineq-date": MIN_DATE,
                        "maxeq-date": MAX_DATE, "_limit": 5},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if items:
                return True
    return False


def load_history_cache():
    cache = {}
    with open(CACHE_PATH, newline="") as f:
        for row in csv.DictReader(f):
            cache[row["station_id"]] = row["date_opened"] or None
    return cache


def main():
    print("Connecting to RDS...", flush=True)
    conn = psycopg2.connect(**DB_CONFIG)
    print("Connected.", flush=True)

    history_cache = load_history_cache()

    with open(ORIGINAL_CSV, newline="") as f:
        original_rows = list(csv.DictReader(f))

    gap_ids = {sid for (_, _, sid) in GAP_STATIONS}
    keepers = [r for r in original_rows if r["station_id"] not in gap_ids]
    already_selected_ids = {r["station_id"] for r in original_rows}

    replacements = []

    with conn.cursor() as cur:
        for area, station_type, old_station_id in GAP_STATIONS:
            print(
                f"\n=== {area} / {station_type}: replacing {old_station_id} ===", flush=True)
            cur.execute(
                """
                SELECT s.station_id, s.station_name, s.lat, s.long
                FROM stations s
                JOIN areas a ON s.area_code = a.area_code
                WHERE a.area_name = %s AND s.station_type = %s AND s.source_api = 'hydrology'
                ORDER BY s.station_id;
                """,
                (area, station_type),
            )
            candidates = cur.fetchall()
            found = False

            for station_id, name, lat, lon in candidates:
                if station_id in already_selected_ids:
                    continue
                date_opened = history_cache.get(station_id)
                if not date_opened or date_opened > MIN_HISTORY_CUTOFF:
                    continue

                time.sleep(0.3)
                if not has_real_data_in_window(station_id):
                    print(
                        f"  skip {name} ({station_id}): no real data in {MIN_DATE}-{MAX_DATE}", flush=True)
                    continue

                print(
                    f"  SELECTED: {name} ({station_id}), opened {date_opened}, confirmed real data", flush=True)
                replacements.append({
                    "area_name": area, "station_type": station_type, "station_id": station_id,
                    "name": name, "lat": lat, "long": lon, "source_api": "hydrology",
                    "date_opened": date_opened,
                })
                already_selected_ids.add(station_id)
                found = True
                break

            if not found:
                print(
                    f"  WARNING: no verified replacement found for {area}/{station_type}.", flush=True)

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
