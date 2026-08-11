"""
08_select_backtest_stations.py

Phase 2, Step 1: Select a representative subset of stations for the
validation backtest.

Scope decision (see DECISIONS.md): the backtest deep-validation is
intentionally NOT national (per ARCHITECTURE.md section 11 — that's
reserved for the live map/personalisation, which stays national since
it's cheap once the geospatial join exists). The backtest instead
targets six areas that had genuine, EA-documented drought episodes in
BOTH 2022 and 2025, giving two independent validation years per area:

    East Anglia
    Wessex
    West Midlands
    Solent and South Downs
    Lincolnshire and Northamptonshire
    Yorkshire (currently "Normal" per EA's Jan 2026 report — useful as
               a recovery/improving-trend case, not just worsening)

For each area, selects up to 3 stations per parameter type
(river_flow, rainfall, groundwater) — ~54 stations total — based on:

  1. Hydrology-sourced only, with a confirmed dateOpened on or before
     2015-01-01 — guarantees real pre-2022 baseline depth and rules out
     flood_monitoring-only stations, which are real-time (~15 min
     refresh) and not part of the historical archive Step 2 pulls from
  2. Geographic spread within the area, via greedy farthest-point
     selection, so the chosen 3 aren't clustered on one river/borehole
     cluster

NOTE ON API FIELD NAMES: Phase 1 found the EA APIs nest or rename
fields more often than the docs suggest (parameter classification
nested under `measures[]`, grid-reference-only coordinates on ~580
groundwater stations, etc.). The `dateOpened` field name below is a
best-guess based on the EA Hydrology API's documented station schema
— confirm against one real response (print `data` for a single
station) before trusting this at scale, same spot-check discipline as
Phase 1.

Run from inside python/, per project convention (see STATUS.md).

Requires: psycopg2, requests
Requires network access to RDS and environment.data.gov.uk — this
script was written but NOT executed in the authoring session (neither
host is reachable from that sandbox); run and sanity-check locally.
"""

import csv
import time
from math import radians, sin, cos, sqrt, atan2

import psycopg2
import requests

# --- config ---
BACKTEST_AREAS = [
    "Cumbria and Lancashire",
    "Devon Cornwall and the Isles of Scilly",
    "East Anglia",
    "East Midlands",
    "Greater Manchester Merseyside and Cheshire",
    "Hertfordshire and North London",
    "Kent South London and East Sussex",
    "Lincolnshire and Northamptonshire",
    "North East",
    "Solent and South Downs",
    "Thames",
    "West Midlands",
    "Wessex",
    "Yorkshire",
]
STATIONS_PER_TYPE = 5  # -> ~15/area x 14 areas = ~210 total (was 3 x 6 = 54)
HYDROLOGY_API_BASE = "https://environment.data.gov.uk/hydrology"

# Only hydrology-sourced stations have a confirmed long-term archive (see
# ARCHITECTURE.md's data source table — Flood-Monitoring is real-time only,
# ~15 min refresh, not a historical archive). Selecting from flood_monitoring
# candidates was pulling in stations with no real multi-year history.
REQUIRE_HYDROLOGY_SOURCE = True

# Candidates must have opened on or before this date to be eligible at all —
# guarantees every selected station has a real pre-2022 baseline period, not
# just "the site with a long-ago date was allowed to lose to a closer-to-
# nothing but more spread-out neighbour."
MIN_HISTORY_CUTOFF = "2015-01-01"

# Local cache of station_id -> history check result, so repeated runs (e.g.
# after tweaking selection logic) don't re-hit the API for ~2,500 stations
# every time. Delete this file if you ever suspect stale/bad cached data.
CACHE_PATH = "station_history_cache.csv"

DB_CONFIG = {
    "host": "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "waterstress",
    "user": "postgres",
    "password": "REDACTED",  # pull from password manager / env var — never hardcode
    "connect_timeout": 10,  # fail fast instead of hanging silently on a blocked connection
}

# Set True for a quick end-to-end smoke test: only processes the first area
# and caps candidates per type at 5, so you can confirm DB + API + CSV
# output all work before committing to the full ~2,500-call run.
TEST_MODE = False


def get_candidate_stations(conn, area_name):
    """Pull all classified stations for one area, with their type."""
    query = """
        SELECT s.station_id, s.station_name, s.station_type, s.lat, s.long, s.source_api
        FROM stations s
        JOIN areas a ON s.area_code = a.area_code
        WHERE a.area_name = %s
    """
    params = [area_name]
    if REQUIRE_HYDROLOGY_SOURCE:
        query += " AND s.source_api = 'hydrology'"
    query += " ORDER BY s.station_type, s.station_id;"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return rows


def load_cache():
    cache = {}
    try:
        with open(CACHE_PATH, newline="") as f:
            for row in csv.DictReader(f):
                cache[row["station_id"]] = {
                    "date_opened": row["date_opened"] or None}
    except FileNotFoundError:
        pass
    return cache


def save_cache(cache):
    with open(CACHE_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["station_id", "date_opened"])
        writer.writeheader()
        for station_id, data in cache.items():
            writer.writerow({"station_id": station_id,
                            "date_opened": data.get("date_opened") or ""})


def check_station_history(station_id, source_api):
    """
    Query the EA Hydrology API for this station's period-of-record.

    VERIFY LIVE before trusting: print a raw response for one station
    first and confirm `dateOpened` is the right field / right shape.
    """
    url = f"{HYDROLOGY_API_BASE}/id/stations/{station_id}.json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [{}])
        item = items[0] if items else {}
        return {"date_opened": item.get("dateOpened")}
    except requests.RequestException as e:
        return {"date_opened": None, "error": str(e)}


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * \
        cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def farthest_point_select(stations, k):
    """
    Greedy farthest-point sampling: start with the station with the
    longest record (earliest dateOpened), then repeatedly add whichever
    remaining station is farthest (max of the min-distance-to-any-
    selected-station) from what's already chosen. Keeps the final set
    geographically spread rather than clustered on one river/borehole
    group. Falls back to "keep everything" if there are <= k candidates.
    """
    if len(stations) <= k:
        return stations

    stations = sorted(stations, key=lambda s: s.get(
        "date_opened") or "9999-99-99")
    selected = [stations[0]]
    remaining = stations[1:]

    while len(selected) < k and remaining:
        best, best_dist = None, -1
        for cand in remaining:
            d = min(
                haversine_km(cand["lat"], cand["long"], s["lat"], s["long"])
                for s in selected
            )
            if d > best_dist:
                best, best_dist = cand, d
        selected.append(best)
        remaining.remove(best)

    return selected


def main():
    print("Connecting to RDS...", flush=True)
    conn = psycopg2.connect(**DB_CONFIG)
    print("Connected.", flush=True)
    selected_all = []

    cache = load_cache()
    print(
        f"Loaded {len(cache)} cached station history result(s) from {CACHE_PATH}.", flush=True)

    areas = BACKTEST_AREAS[:1] if TEST_MODE else BACKTEST_AREAS

    for area in areas:
        print(f"\n=== {area} ===", flush=True)
        candidates = get_candidate_stations(conn, area)

        by_type = {}
        for station_id, name, stype, lat, lon, source_api in candidates:
            by_type.setdefault(stype, []).append({
                "station_id": station_id,
                "name": name,
                "station_type": stype,
                "lat": lat,
                "long": lon,
                "source_api": source_api,
            })

        if not by_type:
            print(f"WARNING: no candidate stations found for area '{area}' — "
                  f"check area_name spelling matches RDS exactly.")
            continue

        for stype, stations in by_type.items():
            if not stations:
                continue

            if TEST_MODE:
                stations = stations[:5]

            print(
                f"  {stype}: checking history for {len(stations)} candidate station(s)...", flush=True)

            # Live history check, using the cache where possible. Only
            # stations not already in the cache cost an API call + sleep.
            new_lookups = 0
            for i, s in enumerate(stations, 1):
                if s["station_id"] in cache:
                    s.update(cache[s["station_id"]])
                else:
                    hist = check_station_history(
                        s["station_id"], s["source_api"])
                    s.update(hist)
                    cache[s["station_id"]] = {
                        "date_opened": hist.get("date_opened")}
                    new_lookups += 1
                    time.sleep(0.2)  # be polite to the free API
                if i % 10 == 0 or i == len(stations):
                    print(
                        f"    ...{i}/{len(stations)} ({new_lookups} new API call(s) so far)", flush=True)

            save_cache(cache)  # persist after every type, not just at the end

            # Drop anything with no confirmed pre-cutoff history — a
            # geographically distant station with an unknown or too-recent
            # dateOpened is not a valid substitute for real baseline depth.
            eligible = [
                s for s in stations
                if s.get("date_opened") and s["date_opened"] <= MIN_HISTORY_CUTOFF
            ]
            dropped = len(stations) - len(eligible)
            if dropped:
                print(f"    dropped {dropped} candidate(s) with no confirmed history "
                      f"before {MIN_HISTORY_CUTOFF} (missing dateOpened or opened too recently)",
                      flush=True)

            chosen = farthest_point_select(eligible, STATIONS_PER_TYPE)
            if len(chosen) < STATIONS_PER_TYPE:
                print(f"NOTE: {area} / {stype} only has {len(chosen)} eligible station(s) "
                      f"after the history-cutoff filter (wanted {STATIONS_PER_TYPE}).")

            for c in chosen:
                c["area_name"] = area
                selected_all.append(c)

    conn.close()

    out_path = "backtest_station_selection_v3_national.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "area_name", "station_type", "station_id", "name",
            "lat", "long", "source_api", "date_opened",
        ])
        writer.writeheader()
        for s in selected_all:
            writer.writerow({
                "area_name": s["area_name"],
                "station_type": s["station_type"],
                "station_id": s["station_id"],
                "name": s["name"],
                "lat": s["lat"],
                "long": s["long"],
                "source_api": s["source_api"],
                "date_opened": s.get("date_opened"),
            })

    print(
        f"Selected {len(selected_all)} stations across {len(BACKTEST_AREAS)} areas.")
    print(
        f"Written to {out_path} — spot-check before Step 2 (historical readings pull).")


if __name__ == "__main__":
    main()
