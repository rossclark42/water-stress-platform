"""
Phase 1, Step 3b - resolve duplicate station_ids found by inspection.
Three cases, handled differently:
  1. Exact duplicate rows (same id, same name, same coords) -> drop the extra copy
  2. Same id, genuinely different coords (>~1km apart) -> these are DIFFERENT
     physical stations that happen to share a code (confirmed real EA data
     quality issue, e.g. E6120 = Brede Sluice Gate AND Northampton Washlands).
     Do NOT merge or silently drop -- make the station_id unique by suffixing
     with source_api, so both real stations survive with traceable IDs.
  3. Same id, same/near-identical coords, different source_api (e.g. Windsor,
     appearing in both flood_monitoring and hydrology) -> same physical station,
     prefer the hydrology row (needed for Phase 2 historical backfill), drop
     the flood_monitoring duplicate.
"""
import pandas as pd

df = pd.read_csv("stations_raw.csv")
print(f"Before dedup: {len(df)} rows")

# Step 1: drop exact full-row duplicates
df = df.drop_duplicates()
print(f"After dropping exact duplicate rows: {len(df)} rows")

# Step 2/3: handle remaining duplicated station_ids
dupe_ids = df[df["station_id"].duplicated(keep=False)]["station_id"].unique()
print(f"\n{len(dupe_ids)} station_ids still duplicated after exact-dupe removal: {list(dupe_ids)}")

rows_to_drop = []
rows_to_rename = {}  # index -> new station_id

for sid in dupe_ids:
    group = df[df["station_id"] == sid]
    # crude distance check in degrees -- ~1km is roughly 0.01 deg at UK latitudes
    lat_range = group["lat"].max() - group["lat"].min()
    long_range = group["long"].max() - group["long"].min()
    same_location = lat_range < 0.01 and long_range < 0.01

    if same_location and set(group["source_api"]) == {"flood_monitoring", "hydrology"}:
        # Case 3: same physical station, prefer hydrology
        drop_idx = group[group["source_api"] == "flood_monitoring"].index
        rows_to_drop.extend(drop_idx)
        print(f"\n{sid}: same station across both APIs -- keeping hydrology row, dropping flood_monitoring")
    else:
        # Case 2: genuinely different physical locations sharing a code -- disambiguate
        print(f"\n{sid}: DIFFERENT physical locations sharing this code -- suffixing to disambiguate")
        for idx, row in group.iterrows():
            new_id = f"{sid}_{row['source_api']}_{idx}"
            rows_to_rename[idx] = new_id
            print(f"  '{row['station_name']}' ({row['lat']}, {row['long']}) -> {new_id}")

df = df.drop(index=rows_to_drop)
for idx, new_id in rows_to_rename.items():
    if idx in df.index:
        df.loc[idx, "station_id"] = new_id

n_remaining_dupes = df["station_id"].duplicated().sum()
print(f"\nFinal: {len(df)} rows, {n_remaining_dupes} duplicate station_ids remaining")

df.to_csv("stations_final_deduped.csv", index=False)
print("Wrote stations_final_deduped.csv")
