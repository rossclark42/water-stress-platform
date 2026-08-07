"""
Phase 1, Step 0 — inspect the EA Public Face Areas source before building any join.
Run locally (this endpoint isn't reachable from Claude's sandbox).
Goal: confirm how to filter EA-only areas from the combined EA+NE layer,
and confirm the CRS reprojection works as expected.
"""
import requests
import geopandas as gpd

BASE = "https://environment.data.gov.uk/arcgis/rest/services/EA/AdminBoundEAandNEpublicFaceAreas/FeatureServer/0/query"

params = {
    "where": "1=1",
    "outFields": "*",
    "returnGeometry": "true",
    "f": "geojson",
    "resultRecordCount": 2000,
}

resp = requests.get(BASE, params=params, timeout=30)
resp.raise_for_status()
gdf = gpd.read_file(resp.text)  # geojson text -> GeoDataFrame

print(f"Total features returned: {len(gdf)}")
print(f"Declared CRS: {gdf.crs}")  # sanity check vs. expected EPSG:27700

# Dump every attribute (minus geometry) so we can see how to split EA vs NE
cols = [c for c in gdf.columns if c != "geometry"]
print("\n--- All records, all non-geometry fields ---")
with __import__("pandas").option_context("display.max_rows", None, "display.max_colwidth", 60):
    print(gdf[cols].sort_values("long_name"))

# Save raw pull locally for reference / diffing later if EA updates the source
gdf.to_file("areas_raw_pull.geojson", driver="GeoJSON")
print("\nSaved raw pull to areas_raw_pull.geojson")
