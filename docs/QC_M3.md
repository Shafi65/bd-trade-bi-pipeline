# M3 QC Verdict — dimensional model

Rows: interim 705,737 -> fact_trade 704,903 (collapsed 834 finer-than-HS8 line items).

- **PASS** — grain is unique (fact_trade key): 0 duplicate keys
- **PASS** — value & quantity conserved vs interim (per year-flow): all 10 year-flows exact
- **PASS** — every fact hs8 resolves in dim_hs: 0 orphans
- **PASS** — every fact country_code resolves in dim_country: 0 orphans
- **PASS** — every (fy, half) in facts has an FX rate: all 10 present
- **PASS** — no negative measures: 0 negatives

## Informational (carried to M5 as view-layer footnotes)

- dim_hs products without a harvested label: 0 of 7,861 (kept, null description)
- dim_hs rows without a chapter name: 0 (hs2 not in reference: [])
- name-keyed countries given surrogate codes: 2
- UNKNOWN-destination fact rows: 141 (BDT 517,486,455; exclude from per-destination views)
- export rows usable for unit price (qty>0 & unit set): 162,864/163,779 (99.4%)

## VERDICT: PASS

