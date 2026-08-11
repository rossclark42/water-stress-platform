-- Phase 1 schema — reference/boundary tables (per SCHEMA.md)
-- Run this against the waterstress DB before loading any data.

CREATE TABLE IF NOT EXISTS areas (
    area_code   TEXT PRIMARY KEY,
    area_name   TEXT NOT NULL,
    geometry    TEXT NOT NULL  -- WKT for now; swap to PostGIS geography type later if needed
);

CREATE TABLE IF NOT EXISTS stations (
    station_id    TEXT PRIMARY KEY,
    station_name  TEXT,
    station_type  TEXT NOT NULL CHECK (station_type IN ('river_flow','rainfall','groundwater')),
    lat           DOUBLE PRECISION NOT NULL,
    long          DOUBLE PRECISION NOT NULL,
    area_code     TEXT REFERENCES areas(area_code),
    source_api    TEXT NOT NULL CHECK (source_api IN ('flood_monitoring','hydrology'))
);

CREATE TABLE IF NOT EXISTS postcode_area_lookup (
    postcode   TEXT PRIMARY KEY,
    lat        DOUBLE PRECISION NOT NULL,
    long       DOUBLE PRECISION NOT NULL,
    area_code  TEXT REFERENCES areas(area_code)
);

-- Reusable nearest-station lookup, built in Phase 1, populated in Phase 5
CREATE TABLE IF NOT EXISTS site_station_lookup (
    postcode    TEXT REFERENCES postcode_area_lookup(postcode),
    station_id  TEXT REFERENCES stations(station_id),
    distance_km DOUBLE PRECISION NOT NULL,
    rank        INTEGER NOT NULL,  -- 1 = nearest
    PRIMARY KEY (postcode, station_id)
);

CREATE INDEX IF NOT EXISTS idx_stations_area ON stations(area_code);
CREATE INDEX IF NOT EXISTS idx_postcode_area ON postcode_area_lookup(area_code);

-- 01_create_readings_table.sql
-- Phase 2: creates the `readings` table designed in SCHEMA.md.
-- Run this once against RDS (DBeaver or psql) before running
-- 09_pull_historical_readings.py.
 
CREATE TABLE IF NOT EXISTS readings (
    reading_id       BIGSERIAL PRIMARY KEY,
    station_id       TEXT NOT NULL REFERENCES stations(station_id),
    reading_datetime TIMESTAMP NOT NULL,
    parameter        TEXT NOT NULL CHECK (parameter IN ('flow', 'rainfall', 'groundwater_level')),
    value            DOUBLE PRECISION,        -- NULL when quality_flag = 'Missing'
    quality_flag     TEXT,                    -- Good / Estimated / Suspect / Unchecked / Missing
    source           TEXT NOT NULL CHECK (source IN ('flood_monitoring_api', 'hydrology_api')),
 
    -- Prevents duplicate rows if the pull script is re-run (idempotent loads).
    CONSTRAINT uq_reading UNIQUE (station_id, reading_datetime, parameter)
);
 
CREATE INDEX IF NOT EXISTS idx_readings_station_param ON readings (station_id, parameter);
CREATE INDEX IF NOT EXISTS idx_readings_datetime ON readings (reading_datetime);
 
-- Note: assumes stations.station_id is TEXT (matches the mixed UUID /
-- short-code IDs seen across both source APIs in Phase 1). If Phase 1's
-- 00_create_schema.sql used a different type for stations.station_id,
-- adjust the FK column type here to match before running.

-- 03_create_seasonal_baselines_table.sql
-- Creates seasonal_baselines, per the design in SCHEMA.md. Run this
-- before compute_seasonal_baselines.sql.

CREATE TABLE IF NOT EXISTS seasonal_baselines (
    station_id      TEXT NOT NULL REFERENCES stations(station_id),
    parameter       TEXT NOT NULL CHECK (parameter IN ('flow', 'rainfall', 'groundwater_level', 'river_level')),
    period          TEXT NOT NULL,  -- day-of-year, '1'-'366'
    baseline_value  DOUBLE PRECISION NOT NULL,
    variability     DOUBLE PRECISION,  -- nullable: stddev is undefined for a single-value bucket

    PRIMARY KEY (station_id, parameter, period)
);

CREATE INDEX IF NOT EXISTS idx_seasonal_baselines_lookup ON seasonal_baselines (station_id, parameter);


-- Phase 2 addition: river_level fallback parameter + flow_measure_type
-- tracking column (see DECISIONS.md for full rationale)
ALTER TABLE readings DROP CONSTRAINT readings_parameter_check;
ALTER TABLE readings ADD CONSTRAINT readings_parameter_check
    CHECK (parameter IN ('flow', 'rainfall', 'groundwater_level', 'river_level'));

ALTER TABLE stations ADD COLUMN IF NOT EXISTS flow_measure_type TEXT
    CHECK (flow_measure_type IN ('flow', 'river_level'));
