# Bangladesh Foreign Trade Analytics — AWS BI Pipeline

End-to-end BI pipeline on AWS analyzing Bangladesh's export prices, product
diversification, and market reach from official **Bangladesh Bureau of Statistics
(BBS) Foreign Trade Statistics** publications.

> 🚧 **Work in progress.** The data pipeline (extraction → reconciliation → dimensional
> model) is complete and verified. The cloud and analysis layers are next.

## The question

Bangladesh's exports have grown — but is the country earning *better prices* for its
goods, or simply selling more units at the same thin margin? That distinction is the
difference between moving up the value chain and running faster to stand still.

Answering it needs price, per product, per destination, over time — which requires
8-digit HS-code detail by country. Only BBS publishes that grain, so this project reads
its publications directly rather than using a cleaner international API that stops at
6 digits.

## What's built so far

**Extraction (`ingestion/`)** — Two parsers turn BBS's published tables into rows.
The 2024-25 workbook is read with `openpyxl`; the 2020-21 → 2023-24 volumes are parsed
from text-based PDFs with `pdfplumber`. Both flatten BBS's hierarchical layout (a
commodity heading followed by unlabeled destination rows) and unpivot the side-by-side
half-years into a tidy long format.

**Reconciliation** — Extraction fails *silently*: a skipped row still looks like a
valid number. So nothing is trusted until it sums back to BBS's own published control
totals, checked at three levels of detail against two independent published tables.
All ten year-flows reconcile at ~0.000%. This runs as a **gate**, not a report.

The checks earned their keep — they surfaced genuine defects in the source
publications (an original print concatenated with a partial revised re-print carrying
different numbers, a country section spliced 500 pages apart, a table truncated
mid-commodity). Each conflict was arbitrated by reconstructing both readings and
keeping the one that matched the publisher's own control totals. See
[docs/DATA_ISSUES.md](docs/DATA_ISSUES.md).

**Dimensional model (`transform/`)** — A star schema in Parquet, partitioned by flow
and fiscal year:

| Table | Grain / contents |
|---|---|
| `fact_trade` | one row per (flow, fiscal year, half, HS8, country, unit) — quantity + value in BDT |
| `dim_hs` | HS8 → HS4 → HS2, product label, chapter name |
| `dim_country` | one authoritative name per country code |
| `dim_fx_rate` | period-average BDT/USD, one per fiscal half |

704,903 fact rows across five fiscal years. Two modeling decisions worth noting: the
fact table stays in the currency it was measured in — USD values and unit prices are
derived in SQL views, never frozen into rows — and numeric keys are canonicalized to a
fixed width, because the source wrote Canada as both `16` and `016` and would otherwise
have split one country's price history in two while every total still added up.

**Quality gate** — [docs/QC_M3.md](docs/QC_M3.md) records the verdict: unique grain,
zero orphaned keys, value and quantity conserved exactly through modeling, full FX
coverage. 96 tests (`pytest`) pin the parser behaviour and the data contracts.

## What's next

- **Data lake** — S3 + Glue Data Catalog (hand-written DDL, no crawlers) + Athena.
- **Analysis SQL** — one view per analytical question: price trends at HS8, biggest
  price improvers, destination-level price deviation, diversification, new-market
  potential.
- **Dashboard** — QuickSight.
- **GenAI trade brief** — an LLM-generated narrative over the findings.

## Notes on the data

- **Values are in taka**, and the taka fell roughly 30% against the dollar over the
  study window — so every price is analyzable in both BDT and USD, via period-average
  rates joined at query time. Otherwise "price improvement" is partly devaluation.
- **Unit prices are only comparable within the same product and unit** (KG, NUM, MTR…).
- At 8-digit HS level, unit value is a *proxy* for price. Heterogeneous "catch-all"
  codes and tiny shipments can produce spectacular but meaningless swings, so the
  analysis applies materiality thresholds.
- Exports are the headline; imports are extracted as supporting context.

## Repo layout

| Path | Purpose |
|---|---|
| `ingestion/` | BBS Excel/PDF extraction → canonical records |
| `transform/` | Cleaning, dimensions, Parquet star schema |
| `sql/` | Glue DDL + Athena analysis queries |
| `tests/` | Parser and data-contract tests |
| `reference/` | Hand-curated reference data (FX rates, HS chapter names) |
| `docs/` | Decision log, data issues, QC verdicts |

Source publications and all generated data are git-ignored; this repo holds the code
and the reasoning, not the data.

## Running it

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python ingestion/extract_fts_excel.py
./.venv/bin/python ingestion/extract_fts_pdf.py
./.venv/bin/python transform/build_warehouse.py
./.venv/bin/python -m pytest
```

The reasoning behind the major decisions is in [docs/DECISIONS.md](docs/DECISIONS.md);
testing approach in [docs/TESTING.md](docs/TESTING.md).
