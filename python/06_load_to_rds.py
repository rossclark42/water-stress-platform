"""
Phase 1, Step 6 — load the three lookup tables into RDS.
Requires 00_create_schema.sql to have already been run.
"""
import os
import pandas as pd
from sqlalchemy import create_engine

PGPASSWORD = os.environ.get("PGPASSWORD")
if not PGPASSWORD:
    raise SystemExit(
        "Set PGPASSWORD environment variable first (do not hardcode it here).")

HOST = "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com"
DB = "waterstress"
USER = "postgres"

engine = create_engine(
    f"postgresql+psycopg2://{USER}:{PGPASSWORD}@{HOST}:5432/{DB}")

# --- areas ---
areas = pd.read_csv("areas.csv")
areas.rename(columns={"geometry_wkt": "geometry"}, inplace=True)
areas[["area_code", "area_name", "geometry"]].to_sql(
    "areas", engine, if_exists="append", index=False
)
print(f"Loaded {len(areas)} rows into areas")

# --- stations ---
stations = pd.read_csv("stations_load_ready.csv")
stations_clean = stations[
    ["station_id", "station_name", "station_type",
        "lat", "long", "area_code", "source_api"]
].dropna(subset=["station_id"])
stations_clean.to_sql("stations", engine, if_exists="append", index=False)
print(f"Loaded {len(stations_clean)} rows into stations")

# --- postcode_area_lookup ---
pc = pd.read_csv("postcode_area_lookup.csv")
pc_clean = pc[["postcode", "lat", "long", "area_code"]].dropna(subset=[
                                                               "postcode"])
pc_clean.to_sql(
    "postcode_area_lookup", engine, if_exists="append", index=False, chunksize=5000, method="multi"
)
print(f"Loaded {len(pc_clean)} rows into postcode_area_lookup")

print("\nDone. Run 07_verify_load.py next to confirm from RDS directly.")
