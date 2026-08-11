-- compute_seasonal_baselines.sql
-- Phase 2 (final step): populates seasonal_baselines from readings.
--
-- Method: for each station/parameter/day-of-year, pool all readings from
-- ANY year within a ±15-day circular window of that day-of-year (circular
-- so e.g. day 3's window correctly reaches back into December, not just
-- clamping at Jan 1). baseline_value = median (robust to skew, standard
-- for hydrological data); variability = stddev, for normalising deviation
-- in Phase 3's percentile/trend calculation.
--
-- Minimum sample size: a bucket needs at least 5 real readings to produce
-- a baseline. Dipped groundwater stations (as few as 20-150 readings
-- across the whole decade) may not clear this for every day-of-year --
-- those buckets are simply not written, rather than producing a
-- baseline built on 1-2 points that would look precise but isn't. Phase 3
-- should handle a missing baseline explicitly (e.g. fall back to a wider
-- window or the station's all-time median) rather than assume one always
-- exists.
--
-- Run once against RDS. Safe to re-run (DELETE + re-INSERT), e.g. after
-- more readings are loaded (West Midlands full-depth pull, any future
-- national expansion).

DELETE FROM seasonal_baselines;

INSERT INTO seasonal_baselines (station_id, parameter, period, baseline_value, variability)
SELECT
    station_id,
    parameter,
    target_day::text AS period,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS baseline_value,
    STDDEV(value) AS variability
FROM (
    SELECT
        r.station_id,
        r.parameter,
        r.value,
        -- Circular day-of-year window: shift the reading's own day-of-year
        -- by each offset in [-15, 15], wrapping around a 366-day year.
        (((EXTRACT(DOY FROM r.reading_datetime)::int - 1 + day_offset) % 366 + 366) % 366) + 1 AS target_day
    FROM readings r
    CROSS JOIN generate_series(-15, 15) AS day_offset
    WHERE r.value IS NOT NULL  -- exclude Missing-quality readings (no value)
) expanded
GROUP BY station_id, parameter, target_day
HAVING COUNT(*) >= 5;

-- Quick sanity check after running: how many station/parameter combos got
-- at least SOME baseline coverage, and how many day-of-year buckets each
-- one actually has (366 = full coverage; less = some days didn't clear
-- the 5-reading minimum, expected for sparse dipped-groundwater stations).
--
-- SELECT station_id, parameter, COUNT(*) AS days_with_baseline
-- FROM seasonal_baselines
-- GROUP BY station_id, parameter
-- ORDER BY days_with_baseline ASC
-- LIMIT 20;