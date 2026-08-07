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