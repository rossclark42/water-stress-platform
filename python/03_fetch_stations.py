"""
Phase 1, Step 3 (final, v2) — adds handling for the rare case where the API
returns lat/long/label as a list (multi-sensor station sharing one code, e.g.
E85123 = 'Ilfracombe Lambda' + 'Ilfracombe Lambda Upstream'). Takes the first
value and flags it, rather than letting it silently poison the column dtype.
"""
import requests
import pandas as pd

RELEVANT_PARAMS = {"level", "flow", "rainfall"}


def first_if_list(value):
    """API occasionally returns a list instead of a scalar for multi-sensor
    stations sharing one code. Take the first value; flag it happened."""
    if isinstance(value, list):
        return value[0], True
    return value, False


def classify_flood_monitoring(station):
    measures = station.get("measures", [])
    if isinstance(measures, dict):
        measures = [measures]
    if not measures:
        return "excluded"
    for m in measures:
        param = m.get("parameter", "").lower()
        qualifier = m.get("qualifier", "").lower()
        if param == "rainfall":
            return "rainfall"
        if param == "flow":
            return "river_flow"
        if param == "level":
            if "groundwater" in qualifier:
                return "groundwater"
            if "tidal" in qualifier:
                return "excluded"
            return "river_flow"
    return "excluded"


def classify_hydrology(station):
    obs_props = station.get("observedProperty", [])
    for op in obs_props:
        uri = op.get("@id", "").lower()
        if "waterflow" in uri:
            return "river_flow"
        if "rainfall" in uri:
            return "rainfall"
        if "groundwater" in uri:
            return "groundwater"
        if "waterlevel" in uri:
            return "river_flow"
    measures = station.get("measures", [])
    for m in measures:
        param = m.get("parameter", "").lower()
        if param == "flow":
            return "river_flow"
        if param == "rainfall":
            return "rainfall"
        if "groundwater" in param:
            return "groundwater"
    return "excluded"


def fetch_flood_monitoring_stations():
    url = "https://environment.data.gov.uk/flood-monitoring/id/stations"
    resp = requests.get(url, params={"_limit": 10000}, timeout=60)
    resp.raise_for_status()
    items = resp.json()["items"]
    rows = []
    multi_value_flags = []
    for s in items:
        lat, lat_was_list = first_if_list(s.get("lat"))
        long_, long_was_list = first_if_list(s.get("long"))
        name, name_was_list = first_if_list(s.get("label"))
        if lat_was_list or long_was_list or name_was_list:
            multi_value_flags.append(s.get("stationReference"))
        rows.append({
            "station_id": s.get("stationReference"),
            "station_name": name,
            "lat": lat,
            "long": long_,
            "station_type": classify_flood_monitoring(s),
            "source_api": "flood_monitoring",
        })
    if multi_value_flags:
        print(f"NOTE: {len(multi_value_flags)} flood-monitoring stations had list-valued "
              f"lat/long/label (multi-sensor codes), took first value: {multi_value_flags}")
    return pd.DataFrame(rows)


def fetch_hydrology_stations():
    url = "https://environment.data.gov.uk/hydrology/id/stations"
    resp = requests.get(url, params={"_limit": 10000}, timeout=60)
    resp.raise_for_status()
    items = resp.json()["items"]
    rows = []
    multi_value_flags = []
    for s in items:
        lat, lat_was_list = first_if_list(s.get("lat"))
        long_, long_was_list = first_if_list(s.get("long"))
        name, name_was_list = first_if_list(s.get("label"))
        if lat_was_list or long_was_list or name_was_list:
            multi_value_flags.append(s.get("notation"))
        rows.append({
            "station_id": s.get("notation") or s.get("stationGuid"),
            "station_name": name,
            "lat": lat,
            "long": long_,
            "station_type": classify_hydrology(s),
            "source_api": "hydrology",
        })
    if multi_value_flags:
        print(f"NOTE: {len(multi_value_flags)} hydrology stations had list-valued "
              f"lat/long/label, took first value: {multi_value_flags}")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    fm = fetch_flood_monitoring_stations()
    print(f"Flood-Monitoring API: {len(fm)} stations")
    print(fm["station_type"].value_counts())

    hy = fetch_hydrology_stations()
    print(f"\nHydrology API: {len(hy)} stations")
    print(hy["station_type"].value_counts())

    combined = pd.concat([fm, hy], ignore_index=True)

    # Now safe to force numeric -- any remaining bad rows get caught explicitly
    combined["lat"] = pd.to_numeric(combined["lat"], errors="coerce")
    combined["long"] = pd.to_numeric(combined["long"], errors="coerce")
    n_bad_coords = combined["lat"].isna().sum() + combined["long"].isna().sum()
    if n_bad_coords:
        print(
            f"WARNING: {n_bad_coords} rows still have unparseable coordinates after cleanup -- inspect these:")
        print(combined[combined["lat"].isna() | combined["long"].isna()])
    combined = combined.dropna(subset=["lat", "long"])

    n_excluded = (combined["station_type"] == "excluded").sum()
    combined = combined[combined["station_type"] != "excluded"]
    print(
        f"\nExcluded {n_excluded} stations (no relevant water/rainfall measure)")

    combined.to_csv("stations_raw.csv", index=False)
    n_dupes = combined["station_id"].duplicated().sum()
    print(
        f"\nWrote stations_raw.csv with {len(combined)} rows ({n_dupes} duplicate station_ids)")
    print("\nFinal station_type breakdown:")
    print(combined["station_type"].value_counts())
