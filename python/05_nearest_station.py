"""
Phase 1, Step 5 — nearest-station logic (reusable for Phase 5 site_station_lookup)
and the Phase 1 exit-test spot-check against known postcodes.
Uses proper haversine distance (km), not raw degree distance.
"""
import numpy as np
import pandas as pd


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between a point and arrays of points."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2)**2
    return 2 * R * np.arcsin(np.sqrt(a))


def nearest_stations(lat, long, stations_df, n=3, station_type=None):
    """Return the n nearest stations to a point, optionally filtered by type."""
    df = stations_df if station_type is None else stations_df[
        stations_df["station_type"] == station_type]
    distances = haversine_km(lat, long, df["lat"].values, df["long"].values)
    df = df.copy()
    df["distance_km"] = distances
    return df.nsmallest(n, "distance_km")[["station_id", "station_name", "station_type", "distance_km"]]


if __name__ == "__main__":
    stations = pd.read_csv("stations_final_deduped.csv")
    postcodes = pd.read_csv("postcode_area_lookup.csv")
    areas = pd.read_csv("areas.csv")[["area_code", "area_name"]]

    # --- Phase 1 exit test: 4 known postcodes, spot-checked against real-world expectation ---
    TEST_POSTCODES = {
        "SW1A 1AA": "Buckingham Palace, London -- expect Hertfordshire and North London or Kent South London and East Sussex",
        "M1 1AE": "Manchester city centre -- expect Greater Manchester Merseyside and Cheshire",
        "EX4 3PB": "Exeter -- expect Devon Cornwall and the Isles of Scilly",
        "YO1 7PR": "York -- expect Yorkshire",
    }

    print("=== Phase 1 exit-test spot-check ===\n")
    for pc, expectation in TEST_POSTCODES.items():
        pc_clean = pc.replace(" ", "").upper()
        row = postcodes[postcodes["postcode"].str.replace(
            " ", "").str.upper() == pc_clean]
        if row.empty:
            print(f"{pc}: NOT FOUND in postcode_area_lookup -- check formatting")
            continue
        row = row.iloc[0]
        area_name = areas[areas["area_code"] ==
                          row["area_code"]]["area_name"].values
        area_name = area_name[0] if len(area_name) else "UNKNOWN"

        print(f"{pc} ({expectation})")
        print(f"  -> assigned area: {row['area_code']} = {area_name}")

        for stype in ["river_flow", "rainfall", "groundwater"]:
            nearest = nearest_stations(
                row["lat"], row["long"], stations, n=1, station_type=stype)
            if len(nearest):
                s = nearest.iloc[0]
                print(
                    f"  -> nearest {stype}: {s['station_name']} ({s['distance_km']:.1f} km)")
            else:
                print(f"  -> nearest {stype}: NONE FOUND")
        print()

    print("=== Coverage check: any area with very few nearby stations of a type? ===")
    # Not exhaustive -- just a sanity pass across the 14 area centroids
    area_geo = pd.read_csv("areas.csv")
    from shapely import wkt
    for _, a in area_geo.iterrows():
        centroid = wkt.loads(a["geometry_wkt"]).centroid
        counts = {}
        for stype in ["river_flow", "rainfall", "groundwater"]:
            near = nearest_stations(
                centroid.y, centroid.x, stations, n=5, station_type=stype)
            within_50km = (near["distance_km"] < 50).sum()
            counts[stype] = within_50km
        print(f"{a['area_name']}: river_flow={counts['river_flow']} rainfall={counts['rainfall']} groundwater={counts['groundwater']} (stations within 50km of area centroid)")
