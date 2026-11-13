# Bangladesh Foreign Trade Analytics — AWS BI Pipeline

[![tests](https://github.com/Shafi65/bd-trade-bi-pipeline/actions/workflows/tests.yml/badge.svg)](https://github.com/Shafi65/bd-trade-bi-pipeline/actions/workflows/tests.yml)

End-to-end BI pipeline on Bangladesh's export prices and diversification, FY2020-21 to
FY2024-25, built from Bangladesh Bureau of Statistics (BBS) trade publications. **Status:** ongoing.

```
BBS PDFs/Excel → Python extraction → reconciliation gate → Parquet star schema
→ S3 → Glue Data Catalog → Athena (SQL views) → statistical tests / QuickSight
```

![QuickSight dashboard](docs/dashboard.png)

## Key findings

- **The 14.2% drop in FY2024-25 exports ($48.9B) is most likely a data artefact.** In 2024, Bangladesh's FY2023-24 exports were found to be overstated by $10-14B through duplicate customs entries. BBS's FY2023-24 total (~$57B) is ~$12.5B above the corrected figure, while for FY2024-25 BBS and the Export Promotion Bureau agree to within ~1% (EPB: $48.28B, +8.6%).
- **Apparel is 80.8% of exports** (HS 61-62), about the same as in FY2020-21 (79.9%).
- **Non-traditional markets pay well below the norm ~8× as often:** among products with falling prices, 6.5% of non-traditional destination prices sit 2+ SD below the product's average, against 0.83% in traditional markets (US, Canada, UK, EU). χ² = 37.8, p < 0.001, n = 1,917 product-destination pairs.

## Data model

Star schema in Parquet on S3, partitioned by `flow` (export/import) and `year`.

| Table | Grain | Contents |
|---|---|---|
| `fact_trade` | flow × fiscal year × half × HS8 × country × unit | `quantity`, `value_bdt` |
| `dim_hs` | HS8 | HS8 → HS4 → HS2, description, chapter |
| `dim_country` | country code | one name per code |
| `dim_fx_rate` | fiscal year × half | average BDT per USD |

Facts stay in taka. USD values and every metric are defined once, in the Athena views ([`sql/analysis_views.sql`](sql/analysis_views.sql)).

## Data quality

The extractors exit with an error unless the data matches BBS's own totals to within 0.1%: the grand total, the Table 04 vs Table 01 totals, and every HS chapter. All ten year-flows pass. The checks surfaced real problems:

- **Source printing defect:** a revised India section was spliced into the 2022-23 imports. After the repair, all 97 chapter totals match.
- **Missing commodities:** older Table 04s omit ~1,100 small commodities, filled from Table 05.
- **Parser bug:** a page footer made chapter 65 read as "1 taka". The parser now uses the H1 + H2 = FY rule to find the right numbers.
- **Integration bug:** country codes were zero-padded differently in the Excel and PDF sources, which would have split Canada in two.

96 tests cover parsing, data snapshots, grain uniqueness, value conservation and referential integrity. The 23 parser tests run in CI. The 73 data tests need the source files, so they run locally.

## Limitations

- Reconciliation proves the extraction matches BBS, not that BBS is right. BBS is compiled from customs records, so it most likely carries the FY2023-24 overstatement.
- Unit price is value ÷ quantity within an HS8 code, so a change in product mix looks like a price change.
- The chi-squared test treats product-destination pairs as independent, which they aren't fully. It also covers a filtered population, which is defined in `v_destination_deviation`.
- Exchange rates are averages of daily market rates, not Bangladesh Bank's official series.

## Run

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # set ATHENA_BUCKET and ATHENA_WORKGROUP

make pipeline   # extract + reconcile -> star schema -> tests (stops on a failed check)
make deploy     # upload to S3, create Glue tables and Athena views
make stats      # statistical tests against the views
```

Source data: BBS Foreign Trade Statistics (FY2020-21 to FY2024-25) from bbs.gov.bd, placed in `Bd_Exportdata/` (not in this repo).

| Path | Contents |
|---|---|
| `ingestion/` | Excel/PDF extraction + reconciliation |
| `transform/` | star schema build + QC |
| `sql/` | Glue DDL, Athena views |
| `scripts/` | Athena runner, QuickSight setup |
| `analysis/` | statistical tests |
| `tests/` | parser and data-contract tests |
| `reference/` | FX rates, HS chapter names |
