"""
Phase 1, Step 3c (v2) - recover stations from OS grid reference strings
(e.g. "TQ3910"), not just easting/northing. Standard OS grid-to-BNG algorithm,
then reproject BNG -> WGS84 same as the areas boundary layer.
"""
import requests
import pandas as pd
from pyproj import Transformer

transformer = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)


def os_gridref_to_easting_northing(gridref):
    """Standard OS grid reference (e.g. 'TQ3910') -> BNG easting/northing."""
    gridref = gridref.replace(" ", "").upper()
    l1 = ord(gridref[0]) - ord("A")
    l2 = ord(gridref[1]) - ord("A")
    if l1 > 7: l1 -= 1  # letter grid skips 'I'
    if l2 > 7: l2 -= 1

    e100k = ((l1 - 2) % 5) * 5 + (l2 % 5)
    n100k = (19 - (l1 // 5) * 5) - (l2 // 5)

    digits = gridref[2:]
    half = len(digits) // 2
    e_digits = digits[:half].ljust(5, "0")
    n_digits = digits[half:].ljust(5, "0")

    easting = e100k * 100000 + int(e_digits)
    northing = n100k * 100000 + int(n_digits)
    return easting, northing


def first_if_list(value):
    return value[0] if isinstance(value, list) else value


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


if __name__ == "__main__":
    resp = requests.get(
        "https://environment.data.gov.uk/flood-monitoring/id/stations",
        params={"_limit": 10000}, timeout=60
    )
    items = resp.json()["items"]

    recovered = []
    unrecoverable = []
    parse_failures = []

    for s in items:
        lat = first_if_list(s.get("lat"))
        long_ = first_if_list(s.get("long"))
        if lat and long_:
            continue  # already has coords, not our concern here

        gridref = s.get("gridReference")
        if not gridref:
            unrecoverable.append(s.get("stationReference"))
            continue

        station_type = classify_flood_monitoring(s)
        if station_type == "excluded":
            continue

        try:
            easting, northing = os_gridref_to_easting_northing(gridref)
            long_wgs84, lat_wgs84 = transformer.transform(easting, northing)
        except Exception as e:
            parse_failures.append((s.get("stationReference"), gridref, str(e)))
            continue

        recovered.append({
            "station_id": s.get("stationReference"),
            "station_name": first_if_list(s.get("label")),
            "lat": lat_wgs84,
            "long": long_wgs84,
            "station_type": station_type,
            "source_api": "flood_monitoring",
        })

    recovered_df = pd.DataFrame(recovered)
    print(f"Recovered {len(recovered_df)} stations via grid reference parsing")
    if len(recovered_df):
        print(recovered_df["station_type"].value_counts())

    print(f"\n{len(unrecoverable)} stations had no gridReference either -- genuinely unrecoverable")
    if parse_failures:
        print(f"{len(parse_failures)} gridReference strings failed to parse:")
        for pf in parse_failures[:10]:
            print(f"  {pf}")

    existing = pd.read_csv("stations_final_deduped.csv")
    print(f"\nExisting deduped file: {len(existing)} rows")

    overlap = set(recovered_df["station_id"]) & set(existing["station_id"]) if len(recovered_df) else set()
    if overlap:
        print(f"WARNING: {len(overlap)} recovered IDs already exist -- dropping overlaps: {overlap}")
        recovered_df = recovered_df[~recovered_df["station_id"].isin(overlap)]

    combined = pd.concat([existing, recovered_df], ignore_index=True)
    combined.to_csv("stations_final_deduped.csv", index=False)

    print(f"\nFinal combined file: {len(combined)} rows (was {len(existing)}, added {len(recovered_df)})")
    print("\nFinal station_type breakdown:")
    print(combined["station_type"].value_counts())

    if len(recovered_df):
        sample = recovered_df.iloc[0]
        print(f"\nSanity check -- '{sample['station_name']}' (gridRef-derived): lat={sample['lat']:.4f}, long={sample['long']:.4f}")
        print("(Should be roughly lat 50-55, long -5 to 2 for England)")
