-- =====================================================================
-- M4 — Glue Data Catalog DDL for the Bangladesh FTS star schema
-- =====================================================================
--
-- WHAT THIS IS
--   Hand-written table definitions that teach Athena how to read the Parquet
--   files M3 wrote. No data is copied or moved: these are EXTERNAL tables —
--   the rows live in S3 as Parquet, and the catalog only stores the schema
--   and the S3 location. Dropping a table deletes the definition, never the data.
--
-- WHY HAND-WRITTEN (no Glue crawler)
--   A crawler is an AWS job that scans S3 and GUESSES a schema. We already KNOW
--   the schema exactly (M3 produced it), so guessing is strictly worse: crawlers
--   cost money per run, can misinfer types (e.g. read a zero-padded code as an
--   int and destroy the leading zero — the exact landmine this project fights),
--   and add a moving part. Writing the DDL by hand is free, deterministic, and
--   reviewable — the industry-standard choice when you control the producer.
--
-- HOW TO RUN
--   Paste into the Athena query editor (region us-east-1) and run each statement,
--   OR run the file with the AWS CLI. Replace REPLACE_ME_BUCKET first (see below).
--
-- ONE VALUE TO REPLACE
--   REPLACE_ME_BUCKET  ->  your S3 bucket name (the only open decision here).
--   The paths under it (processed/trade/..., processed/dims/...) mirror the local
--   data/processed/ layout exactly, so `aws s3 sync data/processed s3://BUCKET/processed`
--   lands everything where these tables expect it. Deploy: scripts/athena_run.py.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Database (a namespace in the Glue Data Catalog; holds no data itself)
-- ---------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS bd_trade
COMMENT 'Bangladesh BBS Foreign Trade Statistics — star schema over S3 Parquet';


-- ---------------------------------------------------------------------
-- fact_trade  — the measures, at one row per unique grain
--   grain = (flow, year, half, hs8, country_code, unit)
--
-- flow and year are PARTITION columns: they are NOT stored inside the Parquet
-- files, they ARE the directory names (flow=X/year=2024-25/...). Athena reads
-- them for free from the path and, when a query filters on them, skips every
-- other folder unread (partition pruning = less scanned = faster + cheaper,
-- since Athena bills per byte scanned).
--
-- PARTITION PROJECTION (the TBLPROPERTIES below) tells Athena the full set of
-- partition values up front, so it never has to list S3 or run `MSCK REPAIR
-- TABLE` to discover partitions — the maintenance-free alternative to a crawler.
-- ---------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS bd_trade.fact_trade (
  half          string  COMMENT 'fiscal half: H1 = Jul-Dec, H2 = Jan-Jun',
  hs8           string  COMMENT '8-digit HS product code, zero-padded; FK -> dim_hs.hs8',
  country_code  string  COMMENT 'destination (X) / source (M) country; FK -> dim_country.country_code',
  unit          string  COMMENT 'quantity unit (KG, NUM, MTR, ...); may be NULL',
  quantity      double  COMMENT 'quantity expressed in `unit`',
  value_bdt     double  COMMENT 'trade value in Bangladeshi taka (BDT)'
)
PARTITIONED BY (
  flow  string  COMMENT 'X = export, M = import',
  year  string  COMMENT 'fiscal year label, e.g. 2024-25'
)
STORED AS PARQUET
LOCATION 's3://REPLACE_ME_BUCKET/processed/trade/'
TBLPROPERTIES (
  'parquet.compression'        = 'SNAPPY',
  'projection.enabled'         = 'true',
  'projection.flow.type'       = 'enum',
  'projection.flow.values'     = 'X,M',
  'projection.year.type'       = 'enum',
  'projection.year.values'     = '2020-21,2021-22,2022-23,2023-24,2024-25',
  'storage.location.template'  = 's3://REPLACE_ME_BUCKET/processed/trade/flow=${flow}/year=${year}'
);


-- ---------------------------------------------------------------------
-- dim_hs  — product dimension (hs8 -> rollup + labels).  Not partitioned.
-- ---------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS bd_trade.dim_hs (
  hs8          string  COMMENT '8-digit HS code (primary key)',
  hs4          string  COMMENT 'first 4 digits (HS heading)',
  hs2          string  COMMENT 'first 2 digits (HS chapter)',
  description  string  COMMENT 'BBS product label; NULL where the source lost it',
  unit         string  COMMENT 'reporting unit for this product; may be NULL',
  chapter_name string  COMMENT 'HS chapter title, joined from reference/hs_chapters.csv'
)
STORED AS PARQUET
LOCATION 's3://REPLACE_ME_BUCKET/processed/dims/dim_hs/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');


-- ---------------------------------------------------------------------
-- dim_country  — one authoritative name per code (+ surrogate keys).
-- ---------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS bd_trade.dim_country (
  country_code string   COMMENT 'BBS numeric code (3-digit), a Uxx surrogate, or UNKNOWN',
  country_name string   COMMENT 'authoritative display name (latest non-null fiscal year)',
  is_surrogate boolean  COMMENT 'true = name-keyed territory assigned a Uxx code'
)
STORED AS PARQUET
LOCATION 's3://REPLACE_ME_BUCKET/processed/dims/dim_country/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');


-- ---------------------------------------------------------------------
-- dim_fx_rate  — BDT per USD, one row per fiscal half (period average).
-- Join on (fy, half) to convert value_bdt -> USD in the analysis views (M5).
-- ---------------------------------------------------------------------
CREATE EXTERNAL TABLE IF NOT EXISTS bd_trade.dim_fx_rate (
  fy          string  COMMENT 'fiscal year, matches fact_trade.year',
  half        string  COMMENT 'fiscal half, matches fact_trade.half',
  bdt_per_usd double  COMMENT 'period-average taka per US dollar for the half',
  source      string  COMMENT 'provenance of the rate'
)
STORED AS PARQUET
LOCATION 's3://REPLACE_ME_BUCKET/processed/dims/dim_fx_rate/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');


-- ---------------------------------------------------------------------
-- SMOKE TEST (run after the tables exist + data is uploaded)
--   Should return the reconciled grand export value for 2024-25 — the same
--   number M3's QC verdict and BBS's own EXPORT TOTAL agree on.
-- ---------------------------------------------------------------------
-- SELECT sum(value_bdt) AS export_value_bdt_2024_25
-- FROM   bd_trade.fact_trade
-- WHERE  flow = 'X' AND year = '2024-25';
