"""
Phase 1, Step 7 — final exit-test verification, querying RDS directly.
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text

PGPASSWORD = os.environ.get("PGPASSWORD")
HOST = "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com"
engine = create_engine(
    f"postgresql+psycopg2://postgres:{PGPASSWORD}@{HOST}:5432/waterstress")

with engine.connect() as conn:
    for table in ["areas", "stations", "postcode_area_lookup"]:
        count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        print(f"{table}: {count} rows")

    print("\n--- areas table (should be exactly 14 rows) ---")
    print(pd.read_sql("SELECT area_code, area_name FROM areas ORDER BY area_name", conn))

    print("\n--- Known-postcode spot-check, queried live from RDS ---")
    test_postcodes = ["SW1A1AA", "M11AE", "EX40AA", "YO17PR"]
    for pc in test_postcodes:
        result = conn.execute(
            text("""
                SELECT p.postcode, p.area_code, a.area_name
                FROM postcode_area_lookup p
                JOIN areas a ON p.area_code = a.area_code
                WHERE REPLACE(p.postcode, ' ', '') = :pc
            """),
            {"pc": pc},
        ).fetchone()
        print(f"  {pc}: {result if result else 'NOT FOUND -- investigate'}")

    print("\n--- Stations per area/type (sanity check) ---")
    print(pd.read_sql("""
        SELECT area_code,
               SUM(CASE WHEN station_type='river_flow' THEN 1 ELSE 0 END) as flow,
               SUM(CASE WHEN station_type='rainfall' THEN 1 ELSE 0 END) as rain,
               SUM(CASE WHEN station_type='groundwater' THEN 1 ELSE 0 END) as gw
        FROM stations GROUP BY area_code ORDER BY area_code
    """, conn))
