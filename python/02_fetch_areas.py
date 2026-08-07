"""
Phase 1, Step 2 — dedupe (dissolve seaward variants), and write clean areas.csv.
CRS is already EPSG:4326 from the live pull -- no reprojection needed.
"""
import geopandas as gpd

# Actual 12 unique area names confirmed from the live pull (2026-08-07) --
# NOTE: ARCHITECTURE.md says 14 EA areas; only 12 exist in the current live data.
# Flagged as a discrepancy to resolve in DECISIONS.md, not silently absorbed.
EA_AREA_NAMES = [
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
    "Wessex",
    "West Midlands",
    "Yorkshire",
]

gdf = gpd.read_file("areas_raw_pull.geojson")
print(f"Loaded {len(gdf)} total features, CRS: {gdf.crs}")

ea = gdf[gdf["long_name"].isin(EA_AREA_NAMES)].copy()
print(
    f"Matched {len(ea)} rows across {ea['long_name'].nunique()} unique area names")

matched_names = set(ea["long_name"])
missing = set(EA_AREA_NAMES) - matched_names
if missing:
    print(f"WARNING: {len(missing)} names did not match: {missing}")

# Dissolve seaward Yes/No pairs into one geometry per area code
dissolved = ea.dissolve(by="code", aggfunc={"long_name": "first"})
dissolved = dissolved.reset_index()
print(
    f"\nAfter dissolve: {len(dissolved)} rows (should equal {ea['long_name'].nunique()})")

out = dissolved[["code", "long_name", "geometry"]].rename(
    columns={"code": "area_code", "long_name": "area_name"}
)
out["geometry_wkt"] = out["geometry"].apply(lambda g: g.wkt)
out[["area_code", "area_name", "geometry_wkt"]].to_csv(
    "areas.csv", index=False)
print(f"\nWrote areas.csv with {len(out)} rows")

centroid = dissolved.iloc[0].geometry.centroid
print(
    f"Sanity check -- {dissolved.iloc[0]['long_name']} centroid: lat={centroid.y:.4f}, long={centroid.x:.4f}")
print("(Should be roughly lat 50-55, long -5 to 2 for England)")
