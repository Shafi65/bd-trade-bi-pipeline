-- =====================================================================
-- M5 — Analysis views over the star schema (Athena / Trino SQL)
-- =====================================================================
--
-- WHAT THIS IS
--   SQL VIEWS that answer the trade expert's questions. A view stores no data —
--   it is a saved query the catalog keeps by name, re-run against the Parquet each
--   time. This is where every derived metric (unit price, USD value, price change)
--   lives: computed once here, never frozen into the fact table. One definition,
--   one place to change it — the metric single-source-of-truth principle.
--
-- WHY VIEWS AND NOT COLUMNS IN THE FACT
--   Unit price = value / quantity and value_usd = value_bdt / rate are DERIVED. If a
--   corrected exchange rate arrives, or we redefine "price change", we edit one view;
--   the 700k-row fact table never moves. Storing derived numbers in facts would mean
--   rewriting everything and risking two disagreeing definitions.
--
-- CONTENTS
--   Foundations: v_unit_price, v_trade_value, v_country_group
--   Headline:    v_export_kpis
--   Q1-Q2:       v_price_trend, v_price_change
--   Q3:          v_destination_deviation
--   Q4:          v_diversification, v_diversification_drivers, v_new_products
--   Q5:          v_market_penetration
--   Every analytical choice is documented at its view. Statistical tests over
--   these views: analysis/stats_tests.py.
--
--   Dialect: Athena engine v3 (Trino). Deploy: scripts/athena_run.py sql/analysis_views.sql
--   Headline numbers cross-checked against an independent pandas implementation.
-- =====================================================================


-- ---------------------------------------------------------------------
-- v_unit_price — the foundation: one row per price-usable fact, with unit
-- price in BOTH currencies and the descriptive attributes joined in.
--
--   * price-usable = quantity > 0 AND unit IS NOT NULL (a unit price exists).
--   * value_usd    = value_bdt / period rate  (join dim_fx_rate on fy+half).
--   * period_ord   = a chronological integer (2020-21 H1 = 1 ... 2024-25 H2 = 10)
--                    so time can be ordered/plotted without string gymnastics.
--   * inner join to dim_fx_rate is lossless (every fy+half has a rate, checked in QC);
--     dims are LEFT joined so a fact is never dropped for a missing label.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_unit_price AS
SELECT
    f.flow,
    f.year                                                     AS fy,
    f.half,
    (CAST(substr(f.year, 1, 4) AS integer) - 2020) * 2
        + (CASE f.half WHEN 'H1' THEN 1 ELSE 2 END)           AS period_ord,
    f.hs8,
    h.hs2,
    h.hs4,
    h.description,
    h.chapter_name,
    f.country_code,
    c.country_name,
    f.unit,
    f.quantity,
    f.value_bdt,
    f.value_bdt / x.bdt_per_usd                               AS value_usd,
    f.value_bdt / f.quantity                                  AS unit_price_bdt,
    (f.value_bdt / x.bdt_per_usd) / f.quantity                AS unit_price_usd
FROM bd_trade.fact_trade      f
JOIN bd_trade.dim_fx_rate     x  ON x.fy = f.year AND x.half = f.half
LEFT JOIN bd_trade.dim_hs     h  ON h.hs8 = f.hs8
LEFT JOIN bd_trade.dim_country c ON c.country_code = f.country_code
WHERE f.quantity > 0 AND f.unit IS NOT NULL;


-- ---------------------------------------------------------------------
-- Q1 — v_price_trend: price trend per good over time (exports).
--
-- Product-level unit price per fiscal half. The correct product price is the
-- QUANTITY-WEIGHTED unit price = SUM(value) / SUM(quantity) across destinations,
-- NOT the average of per-destination unit prices (which would let a tiny shipment
-- swing the number). Grouped by hs8 + unit because unit prices are only comparable
-- within the same unit.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_price_trend AS
SELECT
    hs8,
    unit,
    min(description)                          AS description,   -- constant per hs8; min = deterministic pick
    min(hs2)                                  AS hs2,
    fy,
    half,
    period_ord,
    sum(value_bdt)                            AS value_bdt,
    sum(value_usd)                            AS value_usd,
    sum(quantity)                             AS quantity,
    sum(value_bdt) / sum(quantity)            AS unit_price_bdt,
    sum(value_usd) / sum(quantity)            AS unit_price_usd
FROM bd_trade.v_unit_price
WHERE flow = 'X'
GROUP BY hs8, unit, fy, half, period_ord;




-- ---------------------------------------------------------------------
-- v_country_group — market segmentation used by every destination view.
--
--   * 'Traditional'     = US, Canada, UK, EU-27: Bangladesh's legacy apparel buyers.
--   * 'Non-traditional' = everything else (India, Japan, Australia, China, ...).
--   * 'Residual'        = BBS catch-all buckets (OTHER CONTRI, OT.E/W.EUROPE, Unknown).
--     Kept in value totals, EXCLUDED from per-destination analysis — they are not
--     a market, so they can't deviate, be penetrated, or be counted as a link.
--   Names are BBS's own (truncated) spellings — CZECHOSLOVAK is BBS's label for Czechia.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_country_group AS
SELECT
    country_code,
    country_name,
    CASE
        WHEN country_code = 'UNKNOWN' OR country_name LIKE 'Unknown%'
          OR country_name IN ('OTHER CONTRI', 'OT.E.EUROPE', 'OT.W.EUROPE')   THEN 'Residual'
        WHEN country_name IN ('UNITD STATES', 'CANADA', 'UNTD KINGDOM',
             'AUSTRIA', 'BELGIUM', 'BULGARIA', 'CROATIA', 'CYPRUS', 'CZECHOSLOVAK',
             'DENMARK', 'ESTONIA', 'FINLAND', 'FRANCE', 'GERMANY', 'GREECE', 'HUNGARY',
             'IRELAND', 'ITALY', 'LATVIA', 'LITHUANIA', 'LUXEMBOURG', 'MALTA',
             'NETHERLANDS', 'POLAND', 'PORTUGAL', 'ROMANIA', 'SLOVAKIA', 'SLOVENIA',
             'SPAIN', 'SWEDEN')                                               THEN 'Traditional'
        ELSE 'Non-traditional'
    END AS market_group
FROM bd_trade.dim_country;


-- ---------------------------------------------------------------------
-- v_trade_value — the VALUE foundation (every fact row, priced or not).
--   v_unit_price drops rows without a usable quantity; value analysis (totals,
--   concentration, diversification) must not, or totals stop reconciling.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_trade_value AS
SELECT
    f.flow,
    f.year                                   AS fy,
    f.half,
    (CAST(substr(f.year, 1, 4) AS integer) - 2020) * 2
        + (CASE f.half WHEN 'H1' THEN 1 ELSE 2 END) AS period_ord,
    f.hs8,
    h.hs4,
    h.hs2,
    h.description,
    h.chapter_name,
    CASE WHEN h.hs2 IN ('61', '62') THEN 'Apparel' ELSE 'Non-apparel' END AS segment,
    f.country_code,
    g.country_name,
    g.market_group,
    f.value_bdt,
    f.value_bdt / x.bdt_per_usd              AS value_usd,
    x.bdt_per_usd
FROM bd_trade.fact_trade        f
JOIN bd_trade.dim_fx_rate       x ON x.fy = f.year AND x.half = f.half
LEFT JOIN bd_trade.dim_hs       h ON h.hs8 = f.hs8
LEFT JOIN bd_trade.v_country_group g ON g.country_code = f.country_code;


-- ---------------------------------------------------------------------
-- v_export_kpis — MBR-style headline metrics per fiscal half (dashboard KPI row).
--   The BDT vs USD growth gap IS the devaluation effect, shown explicitly.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_export_kpis AS
WITH p AS (
    SELECT fy, half, period_ord,
           sum(value_bdt)                                        AS export_bdt,
           sum(value_usd)                                        AS export_usd,
           sum(CASE WHEN segment = 'Apparel' THEN value_usd END) AS apparel_usd,
           count(DISTINCT hs8)                                   AS n_products,
           count(DISTINCT CASE WHEN market_group <> 'Residual' THEN country_code END) AS n_markets,
           max(bdt_per_usd)                                      AS bdt_per_usd
    FROM bd_trade.v_trade_value
    WHERE flow = 'X'
    GROUP BY fy, half, period_ord
)
SELECT p.*,
       concat(fy, ' ', half)                                            AS period_label, -- display label; sorts chronologically
       apparel_usd / export_usd                                         AS apparel_share,
       export_usd / lag(export_usd, 2) OVER (ORDER BY period_ord) - 1   AS yoy_usd,  -- same half, prior year
       export_bdt / lag(export_bdt, 2) OVER (ORDER BY period_ord) - 1   AS yoy_bdt
FROM p;


-- ---------------------------------------------------------------------
-- Q2 — v_price_change: which goods achieved the biggest price improvements.
--
-- DECISIONS:
--   * USD, not BDT — strips the taka devaluation (83 -> 122 BDT per USD; the taka lost ~32% of its dollar value), so this
--     is real price movement, not exchange-rate drift.
--   * Material years only (>= $100k that year) and >= 3 of them, so no single
--     negligible lot sets an endpoint (the HS 84802000 +19,000% artifact).
--   * trend_cagr_usd = exp(slope of ln(price) on year) - 1: a LOG-LINEAR TREND fit
--     across ALL material years. Unlike first-vs-last % change, one odd endpoint
--     can't dominate it. This is the ranking metric; pct_change_usd is kept for
--     intuition.
--   * is_catch_all flags heterogeneous "OTHER..." / n.e.s. codes. Their unit value
--     is a product MIX, not a price, so the ranking excludes them (WHERE NOT
--     is_catch_all) — 26% of codes but only ~6% of export value.
--     BBS abbreviates: OTHER / OTHERS / OTR / UNKNOWN are all matched as words.
--   * is_volatile = max/min yearly price > 4x. A real product's price doesn't
--     swing 4x in five years; when it does, the code's physical mix changed (or a
--     unit was misreported). Ranked "price" views exclude both flags.
--   * qty_cagr = the same trend fit on volume, for the price-vs-volume correlation.
--   No ORDER BY: a view's row order is not guaranteed; consumers sort.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_price_change AS
WITH yearly AS (
    SELECT
        hs8,
        unit,
        fy,
        CAST(substr(fy, 1, 4) AS integer)     AS fy_start,
        min(description)                      AS description,
        min(hs2)                              AS hs2,
        min(hs4)                              AS hs4,
        min(chapter_name)                     AS chapter_name,
        sum(value_usd)                        AS value_usd,
        sum(quantity)                         AS quantity,
        sum(value_usd) / sum(quantity)        AS unit_price_usd
    FROM bd_trade.v_unit_price
    WHERE flow = 'X'
    GROUP BY hs8, unit, fy
    HAVING sum(value_usd) >= 100000
),
endpoints AS (
    SELECT
        hs8,
        unit,
        min(description)                        AS description,
        min(hs2)                                AS hs2,
        min(hs4)                                AS hs4,
        min(chapter_name)                       AS chapter_name,
        min_by(unit_price_usd, fy_start)        AS first_price_usd,
        max_by(unit_price_usd, fy_start)        AS last_price_usd,
        min_by(fy, fy_start)                    AS first_fy,
        max_by(fy, fy_start)                    AS last_fy,
        max_by(value_usd, fy_start)             AS last_value_usd,
        sum(value_usd)                          AS total_value_usd,
        count(*)                                AS material_years,
        max(unit_price_usd) / min(unit_price_usd) AS price_range_ratio,
        regr_slope(ln(unit_price_usd), fy_start) AS log_price_slope,
        regr_slope(ln(quantity), fy_start)      AS log_qty_slope
    FROM yearly
    GROUP BY hs8, unit
)
SELECT
    hs8,
    unit,
    description,
    hs4,
    hs2,
    chapter_name,
    CASE WHEN hs2 IN ('61', '62') THEN 'Apparel' ELSE 'Non-apparel' END AS segment,
    (description IS NULL
     OR regexp_like(upper(description), '(^|[^A-Z])(OTHERS?|OTR|UNKNOWN)([^A-Z]|$)')
     OR upper(description) LIKE '%N.E.S%'
     OR upper(description) LIKE '%NOT ELSEWHERE%')        AS is_catch_all,
    price_range_ratio > 4                                  AS is_volatile,
    first_fy,
    last_fy,
    material_years,
    first_price_usd,
    last_price_usd,
    (last_price_usd - first_price_usd) / first_price_usd   AS pct_change_usd,
    exp(log_price_slope) - 1                               AS trend_cagr_usd,
    exp(log_qty_slope) - 1                                 AS qty_cagr,
    last_value_usd,
    total_value_usd
FROM endpoints
WHERE material_years >= 3;


-- ---------------------------------------------------------------------
-- Q3 — v_destination_deviation: for price-ERODED goods, which destinations
-- pay significantly less (or more) than the product's typical price.
--
-- DECISIONS:
--   * eroded = clean (not catch-all, not volatile) product with trend_cagr_usd < 0 (Q2's definition).
--   * evaluated in the product's latest material year (the current state).
--   * a destination counts only with >= $50k of that product that year
--     (small lots are noise — the same trap as Q2).
--   * z-score on LN(unit price) across destinations: prices are right-skewed,
--     logs make the distribution roughly symmetric so |z| is meaningful.
--   * requires >= 8 qualifying destinations. With n points the largest possible
--     |z| is (n-1)/sqrt(n): at n=5 that's 1.79, so |z| >= 2 could never fire.
--   * pct_dev_vs_product = vs the quantity-weighted product price (intuitive "%
--     below average"); flag = |z| >= 2 (outlying at ~95% under normality).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_destination_deviation AS
WITH eroded AS (
    SELECT hs8, unit, last_fy, trend_cagr_usd
    FROM bd_trade.v_price_change
    WHERE NOT is_catch_all AND NOT is_volatile AND trend_cagr_usd < 0
),
dest AS (
    SELECT
        p.hs8, p.unit, p.fy, p.country_code,
        min(p.country_name)                   AS country_name,
        min(p.description)                    AS description,
        min(g.market_group)                   AS market_group,
        sum(p.value_usd)                      AS value_usd,
        sum(p.quantity)                       AS quantity,
        sum(p.value_usd) / sum(p.quantity)    AS unit_price_usd
    FROM bd_trade.v_unit_price p
    JOIN eroded e  ON e.hs8 = p.hs8 AND e.unit = p.unit AND e.last_fy = p.fy
    JOIN bd_trade.v_country_group g ON g.country_code = p.country_code
    WHERE p.flow = 'X' AND g.market_group <> 'Residual'
    GROUP BY p.hs8, p.unit, p.fy, p.country_code
    HAVING sum(p.value_usd) >= 50000
),
prod AS (
    SELECT hs8, unit,
           sum(value_usd) / sum(quantity)     AS product_price_usd,
           avg(ln(unit_price_usd))            AS mean_log_price,
           stddev_samp(ln(unit_price_usd))    AS sd_log_price,
           count(*)                           AS n_dest
    FROM dest
    GROUP BY hs8, unit
)
SELECT
    d.hs8, d.unit, d.description, d.fy, d.country_code, d.country_name, d.market_group,
    d.value_usd, d.quantity, d.unit_price_usd,
    p.product_price_usd,
    p.n_dest,
    e.trend_cagr_usd                                              AS product_trend_cagr_usd,
    d.unit_price_usd / p.product_price_usd - 1                    AS pct_dev_vs_product,
    (ln(d.unit_price_usd) - p.mean_log_price) / p.sd_log_price    AS z_score,
    abs((ln(d.unit_price_usd) - p.mean_log_price) / p.sd_log_price) >= 2 AS is_significant
FROM dest d
JOIN prod p   ON p.hs8 = d.hs8 AND p.unit = d.unit
JOIN eroded e ON e.hs8 = d.hs8 AND e.unit = d.unit
WHERE p.n_dest >= 8 AND p.sd_log_price > 0;


-- ---------------------------------------------------------------------
-- Q4a — v_diversification: is the export basket diversifying? (per fiscal year)
--
--   HHI (Herfindahl-Hirschman Index) = 10,000 x sum(share^2). 10,000 = one product
--   is everything; near 0 = perfectly spread. Antitrust convention: < 1,500
--   unconcentrated, 1,500-2,500 moderate, > 2,500 high. Computed at HS4 (product
--   heading) — at HS8 trivially fine codes split one real product into many.
--   Market HHI = the same index over destinations (market diversification).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_diversification AS
WITH prod AS (
    SELECT fy, hs4, sum(value_usd) AS v
    FROM bd_trade.v_trade_value WHERE flow = 'X'
    GROUP BY fy, hs4
),
prod_s AS (
    SELECT fy, hs4, v,
           v / sum(v) OVER (PARTITION BY fy)                         AS s,
           row_number() OVER (PARTITION BY fy ORDER BY v DESC)       AS rk
    FROM prod
),
mkt AS (
    SELECT fy, country_code, sum(value_usd) AS v
    FROM bd_trade.v_trade_value WHERE flow = 'X' AND market_group <> 'Residual'
    GROUP BY fy, country_code
),
mkt_s AS (
    SELECT fy, v / sum(v) OVER (PARTITION BY fy) AS s FROM mkt
),
seg AS (
    SELECT fy,
           sum(CASE WHEN segment = 'Apparel' THEN value_usd END) / sum(value_usd)          AS apparel_share,
           sum(CASE WHEN market_group = 'Non-traditional' THEN value_usd END)
             / sum(CASE WHEN market_group <> 'Residual' THEN value_usd END)                AS nontrad_market_share
    FROM bd_trade.v_trade_value WHERE flow = 'X'
    GROUP BY fy
)
SELECT
    p.fy,
    p.total_usd,
    p.hhi_product_hs4,
    p.top5_hs4_share,
    p.n_hs4_over_1m,
    m.hhi_market,
    s.apparel_share,
    s.nontrad_market_share
FROM (
    SELECT fy, sum(v) AS total_usd, 10000 * sum(s * s) AS hhi_product_hs4,
           sum(CASE WHEN rk <= 5 THEN s END) AS top5_hs4_share,
           count_if(v >= 1e6) AS n_hs4_over_1m
    FROM prod_s GROUP BY fy
) p
JOIN (SELECT fy, 10000 * sum(s * s) AS hhi_market FROM mkt_s GROUP BY fy) m ON m.fy = p.fy
JOIN seg s ON s.fy = p.fy;


-- ---------------------------------------------------------------------
-- Q4b — v_diversification_drivers: WHICH goods drive diversification.
--   Non-apparel HS4 headings ranked by USD export growth FY2020-21 -> FY2024-25,
--   with each heading's share of total non-apparel growth. Apparel is excluded by
--   definition: growth there deepens concentration, it doesn't diversify.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_diversification_drivers AS
WITH h AS (
    SELECT hs4, min(hs2) AS hs2, min(chapter_name) AS chapter_name,
           min_by(description, length(description)) AS sample_description,
           sum(CASE WHEN fy = '2020-21' THEN value_usd ELSE 0 END) AS usd_2020_21,
           sum(CASE WHEN fy = '2024-25' THEN value_usd ELSE 0 END) AS usd_2024_25
    FROM bd_trade.v_trade_value
    WHERE flow = 'X' AND segment = 'Non-apparel'
    GROUP BY hs4
)
SELECT h.*,
       usd_2024_25 - usd_2020_21                                            AS usd_growth,
       (usd_2024_25 - usd_2020_21) / sum(usd_2024_25 - usd_2020_21) OVER () AS share_of_nonapparel_growth
FROM h;


-- ---------------------------------------------------------------------
-- Q4c — v_new_products: successful NEW export products.
--   new        = < $100k in BOTH baseline years (FY20-21, FY21-22): absent or trivial;
--   successful = >= $1M in FY2024-25.
--   Two baseline years (not one) so a product that merely dipped once isn't "new".
--   CAVEAT — HS 2022 revision: the WCO renumbered parts of the HS nomenclature from
--   2022, so some codes are "new" only because an existing product moved to a new
--   code (BBS often has no label for these -> 'UNKNOWN'). is_catch_all flags them;
--   the Q5 ranking should use clean (NOT is_catch_all) products only.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_new_products AS
WITH y AS (
    SELECT hs8,
           min(description) AS description, min(hs4) AS hs4, min(hs2) AS hs2,
           min(chapter_name) AS chapter_name, min(segment) AS segment,
           sum(CASE WHEN fy = '2020-21' THEN value_usd ELSE 0 END) AS usd_2020_21,
           sum(CASE WHEN fy = '2021-22' THEN value_usd ELSE 0 END) AS usd_2021_22,
           sum(CASE WHEN fy = '2022-23' THEN value_usd ELSE 0 END) AS usd_2022_23,
           sum(CASE WHEN fy = '2023-24' THEN value_usd ELSE 0 END) AS usd_2023_24,
           sum(CASE WHEN fy = '2024-25' THEN value_usd ELSE 0 END) AS usd_2024_25,
           count(DISTINCT CASE WHEN fy = '2024-25' AND market_group <> 'Residual'
                               THEN country_code END)              AS n_markets_2024_25
    FROM bd_trade.v_trade_value
    WHERE flow = 'X'
    GROUP BY hs8
)
SELECT y.*,
       (description IS NULL
        OR regexp_like(upper(description), '(^|[^A-Z])(OTHERS?|OTR|UNKNOWN)([^A-Z]|$)')) AS is_catch_all
FROM y
WHERE usd_2020_21 < 100000 AND usd_2021_22 < 100000 AND usd_2024_25 >= 1000000;


-- ---------------------------------------------------------------------
-- Q5 — v_market_penetration: new-market headroom for the successful new products.
--
--   HONEST LIMIT: the right denominator is WORLD import demand per HS code, which
--   BBS data cannot see (the deferred UN Comtrade stretch goal). This view uses the
--   best WITHIN-BBS proxy: "adjacent" markets = destinations that already buy a
--   SIBLING product (same HS4 heading) from Bangladesh in FY2024-25 but do not yet
--   buy this HS8. They have proven buyers, importers and logistics for the heading.
--   headroom_usd = those markets' FY24-25 spend on the heading — a sizing signal.
--   Threshold: a market "buys" a product at >= $10k in the year.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW bd_trade.v_market_penetration AS
WITH links AS (
    SELECT hs8, hs4, country_code, min(country_name) AS country_name, sum(value_usd) AS v
    FROM bd_trade.v_trade_value
    WHERE flow = 'X' AND fy = '2024-25' AND market_group <> 'Residual'
    GROUP BY hs8, hs4, country_code
    HAVING sum(value_usd) >= 10000
),
heading_mkts AS (
    SELECT hs4, country_code, min(country_name) AS country_name, sum(v) AS heading_usd
    FROM links GROUP BY hs4, country_code
),
adjacent AS (
    SELECT n.hs8, hm.country_code, hm.country_name, hm.heading_usd
    FROM bd_trade.v_new_products n
    JOIN heading_mkts hm ON hm.hs4 = n.hs4
    WHERE NOT EXISTS (SELECT 1 FROM links l
                      WHERE l.hs8 = n.hs8 AND l.country_code = hm.country_code)
)
SELECT
    n.hs8, n.description, n.hs4, n.chapter_name, n.segment, n.is_catch_all,
    n.usd_2024_25,
    n.n_markets_2024_25,
    count(a.country_code)                                                   AS n_adjacent_markets,
    coalesce(sum(a.heading_usd), 0)                                         AS headroom_usd,
    array_join(slice(array_agg(a.country_name ORDER BY a.heading_usd DESC)
                     FILTER (WHERE a.country_name IS NOT NULL), 1, 5), ', ') AS top_adjacent_markets
FROM bd_trade.v_new_products n
LEFT JOIN adjacent a ON a.hs8 = n.hs8
GROUP BY n.hs8, n.description, n.hs4, n.chapter_name, n.segment, n.is_catch_all, n.usd_2024_25, n.n_markets_2024_25;


-- ---------------------------------------------------------------------
-- Presentation views — small, dashboard-ready slices of the views above.
-- The filtering/ranking logic lives here (governed, testable SQL), so the
-- dashboard only displays; no hidden filters or calculated fields in the BI tool.
-- ---------------------------------------------------------------------

-- Q2 for the dashboard: top 10 price gainers + top 10 decliners among clean,
-- material products (>= $5M exports in their latest year).
CREATE OR REPLACE VIEW bd_trade.v_price_movers AS
WITH clean AS (
    SELECT hs8, unit, description, segment, trend_cagr_usd, last_value_usd,
           row_number() OVER (ORDER BY trend_cagr_usd DESC) AS rk_up,
           row_number() OVER (ORDER BY trend_cagr_usd ASC)  AS rk_down
    FROM bd_trade.v_price_change
    WHERE NOT is_catch_all AND NOT is_volatile AND last_value_usd >= 5000000
)
SELECT hs8, unit,
       concat(substr(description, 1, 30), ' (', hs8, ')')   AS product,
       segment, trend_cagr_usd, last_value_usd,
       CASE WHEN rk_up <= 10 THEN 'Top gainer' ELSE 'Top decliner' END AS mover_type
FROM clean
WHERE rk_up <= 10 OR rk_down <= 10;

-- Q3 for the dashboard: share of destination prices significantly BELOW the
-- product norm (z <= -2), by market group. Same population as the chi-squared test.
CREATE OR REPLACE VIEW bd_trade.v_market_discount AS
SELECT market_group,
       count(*)                                          AS product_destination_pairs,
       count_if(is_significant AND z_score < 0)          AS significantly_low,
       CAST(count_if(is_significant AND z_score < 0) AS double) / count(*) AS low_price_rate
FROM bd_trade.v_destination_deviation
GROUP BY market_group;


-- Headline KPI row: one row, one column per KPI card. Every figure is read from an
-- existing view, so the cards can never disagree with the charts below them.
CREATE OR REPLACE VIEW bd_trade.v_headline_kpis AS
WITH yr AS (
    SELECT fy, sum(export_usd) AS usd, sum(export_bdt) AS bdt
    FROM bd_trade.v_export_kpis GROUP BY fy
),
latest AS (SELECT max(fy) AS fy FROM yr)
SELECT
    l.fy                                                       AS latest_fy,
    cur.usd                                                    AS export_usd,
    cur.usd / prev.usd - 1                                     AS yoy_usd,
    cur.bdt / prev.bdt - 1                                     AS yoy_bdt,
    d.apparel_share,
    (SELECT low_price_rate FROM bd_trade.v_market_discount
      WHERE market_group = 'Non-traditional')                  AS low_price_rate_nontrad,
    (SELECT low_price_rate FROM bd_trade.v_market_discount
      WHERE market_group = 'Traditional')                      AS low_price_rate_trad
FROM latest l
JOIN yr cur  ON cur.fy = l.fy
JOIN yr prev ON CAST(substr(prev.fy, 1, 4) AS integer) = CAST(substr(l.fy, 1, 4) AS integer) - 1
JOIN bd_trade.v_diversification d ON d.fy = l.fy;
