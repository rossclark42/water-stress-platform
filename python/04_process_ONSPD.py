"""
Phase 1, Step 4 (final) — process ONSPD into a full-postcode->area lookup.
Filters to live England postcodes, drops sentinel/invalid coordinates,
joins each postcode directly against the 14 EA areas (no district aggregation).
"""
import pandas as pd
import geopandas as gpd
from shapely import wkt
from shapely.geometry import Point

ONSPD_PATH = "/Users/rossclark/Sigma Labs/Personal Project/ONSPD_MAY_2026/Data/ONSPD_MAY_2026_UK.csv"

USECOLS = ["pcds", "doterm", "lat", "long", "ctry25cd"]

print("Loading ONSPD (this is a ~2.7M row file, may take a minute)...")
df = pd.read_csv(ONSPD_PATH, usecols=USECOLS, low_memory=False)
print(f"Loaded {len(df)} total UK postcode records")

# --- Filter: live only ---
live = df[df["doterm"].isna()].copy()
print(f"Live postcodes: {len(live)}")

# --- Filter: England only ---
england = live[live["ctry25cd"] == "E92000001"].copy()
print(f"Live England postcodes: {len(england)}")

england = england.dropna(subset=["lat", "long"])
print(f"With valid lat/long: {len(england)}")

# --- Drop sentinel/invalid coordinates. ONSPD uses lat=99.999999, long=0
# for postcodes with no valid grid reference (PO boxes, non-geographic
# codes, some overseas/BFPO addresses). At full-postcode level these are
# just dropped outright -- no averaging step for them to silently corrupt. ---
before = len(england)
england = england[(england["lat"] < 90) & (
    england["lat"] > 49) & (england["long"].abs() < 10)]
print(f"Dropped {before - len(england)} rows with sentinel/invalid coordinates")

# --- Join against EA areas ---
areas_df = pd.read_csv("areas.csv")
areas_df["geometry"] = areas_df["geometry_wkt"].apply(wkt.loads)
areas = gpd.GeoDataFrame(areas_df, geometry="geometry", crs="EPSG:4326")

print(
    f"\nBuilding GeoDataFrame for {len(england)} postcodes and joining against {len(areas)} areas...")
postcodes_gdf = gpd.GeoDataFrame(
    england[["pcds", "lat", "long"]],
    geometry=[Point(xy) for xy in zip(england["long"], england["lat"])],
    crs="EPSG:4326",
)

joined = gpd.sjoin(
    postcodes_gdf, areas[["area_code", "geometry"]], how="left", predicate="within")

n_unmatched = joined["area_code"].isna().sum()
print(f"\nPostcodes matched to an area: {len(joined) - n_unmatched}")
print(f"Postcodes NOT matched: {n_unmatched}")

if n_unmatched > 0:
    print("\nSample of unmatched postcodes (should be a small number -- coastal/edge cases):")
    print(joined[joined["area_code"].isna()]
          [["pcds", "lat", "long"]].head(20).to_string(index=False))

out = joined.drop(columns=["geometry", "index_right"]
                  ).rename(columns={"pcds": "postcode"})
out.to_csv("postcode_area_lookup.csv", index=False)
print(f"\nWrote postcode_area_lookup.csv with {len(out)} rows")

print("\nArea distribution (sanity check -- no area should be wildly over/under-represented):")
print(out["area_code"].value_counts())
