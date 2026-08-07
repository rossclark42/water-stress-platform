import pandas as pd

"""
Phase 1, Step 5c - drop the 23 stations outside England (Scotland/Wales border
catchments monitored by EA but outside the 14 operational areas), producing
the final load-ready stations table.
"""

df = pd.read_csv("stations_final_with_area.csv")
before = len(df)
df_clean = df.dropna(subset=["area_code"])
print(f"Dropped {before - len(df_clean)} stations outside England (Scotland/Wales border catchments)")
df_clean.to_csv("stations_load_ready.csv", index=False)
print(f"Wrote stations_load_ready.csv with {len(df_clean)} rows")
