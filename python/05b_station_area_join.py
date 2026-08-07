from shapely.geometry import Point
from shapely import wkt
import geopandas as gpd
import pandas as pd

"""
Phase 1, Step 5b — the station->area point-in-polygon join that SCHEMA.md's
stations table requires (area_code FK), dropped accidentally when the join
script was rewritten around nearest-station logic.
"""

stations = pd.read_csv("stations_final_deduped.csv")
areas_df = pd.read_csv("areas.csv")
areas_df["geometry"] = areas_df["geometry_wkt"].apply(wkt.loads)
areas = gpd.GeoDataFrame(areas_df, geometry="geometry", crs="EPSG:4326")

stations_gdf = gpd.GeoDataFrame(
    stations,
    geometry=[Point(xy) for xy in zip(stations["long"], stations["lat"])],
    crs="EPSG:4326",
)

joined = gpd.sjoin(
    stations_gdf, areas[["area_code", "geometry"]], how="left", predicate="within")

n_unmatched = joined["area_code"].isna().sum()
print(
    f"Stations matched to an area: {len(joined) - n_unmatched} / {len(joined)}")
print(f"Unmatched: {n_unmatched}")

if n_unmatched > 0:
    print("\nSample unmatched (expected for coastal/estuary stations right on a boundary):")
    print(joined[joined["area_code"].isna()][[
          "station_id", "station_name", "lat", "long"]].head(15).to_string(index=False))

out = joined.drop(columns=["geometry", "index_right"])
out.to_csv("stations_final_with_area.csv", index=False)
print(f"\nWrote stations_final_with_area.csv with {len(out)} rows")

print("\nStations per area (sanity check):")
print(out.groupby(["area_code", "station_type"]).size().unstack(fill_value=0))
