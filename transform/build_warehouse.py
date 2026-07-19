"""
M3 — Turn the reconciled interim facts into a dimensional model on disk (Parquet).

WHAT THIS MILESTONE IS (the frame for an interview)
---------------------------------------------------
M1/M2 produced five interim CSVs that are CORRECT (every year-flow ties to BBS's
published totals) but not MODELLED — they are still shaped like the source documents:
the grain is not unique, country names are repeated on every row, there are no
dimensions, and there is no FX. M3 reshapes that into a STAR SCHEMA:

    fact_trade   — keys + measures only, one row per unique grain
      grain = (flow, fy, half, hs8, country_code, unit)
      measures = quantity, value_bdt
    dim_hs       — hs8 -> hs4 -> hs2 -> description, unit, chapter_name
    dim_country  — country_code -> authoritative name (+ surrogate keys)
    dim_fx_rate  — (fy, half) -> BDT per USD (Bangladesh Bank period average)

The fact stays PURE BDT. Unit price and value_usd are DERIVED in SQL views (M5) by
joining dim_fx_rate — a single source of truth for every metric, so a corrected rate
means editing one dim row, never rewriting facts. This is the textbook reason
derived metrics live in views, not the fact table.

Output is Parquet partitioned by flow and year (the on-disk layout S3 + Athena want):
    data/processed/trade/flow=<X|M>/year=<FY>/*.parquet   (facts)
    data/processed/dims/<dim_name>/*.parquet              (one folder per dim = one
                                                           future Glue table)
Partition columns (flow, year) live in the PATH only, not inside the files — that is
the Hive/Athena convention: the directory name IS the column value, read for free.

The run ends with a QC verdict (printed + docs/QC_M3.md). That verdict is the M3
decision gate: the owner signs it off before ANY AWS resource is created.

Run:    ./.venv/bin/python transform/build_warehouse.py
Inputs: data/interim/fts_<FY>_facts.csv, data/interim/fts_<FY>_hs_desc.csv,
        reference/fx_bdt_usd.csv, reference/hs_chapters.csv
"""

from pathlib import Path
import re
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "data" / "interim"
REFERENCE = ROOT / "reference"
PROCESSED = ROOT / "data" / "processed"
TRADE_DIR = PROCESSED / "trade"
DIMS_DIR = PROCESSED / "dims"
QC_DOC = ROOT / "docs" / "QC_M3.md"

FISCAL_YEARS = ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25"]

# The declared grain: one fact row = one (flow, fy, half, product, destination, unit).
GRAIN = ["flow", "fy", "half", "hs8", "country_code", "unit"]
MEASURES = ["quantity", "value_bdt"]

# A real BBS country code is a run of digits we normalise to exactly three (see
# normalize_country_code). Anything left non-numeric is either a name-keyed country
# (BBS printed a name but no numeric code, e.g. WEST.SAHARA) or the UNKNOWN sentinel —
# both handled explicitly below.
NUMERIC_CC = re.compile(r"^\d{3}$")
ANY_DIGITS = re.compile(r"^\d+$")
UNKNOWN_CC = "UNKNOWN"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_interim():
    """Read the five fact CSVs into one frame; hs8/country_code stay STRINGS.

    Reading codes as strings is not optional: pandas would infer "01012100" as the
    int 1012100 and silently destroy the leading zero the whole pipeline depends on.
    """
    frames = []
    for fy in FISCAL_YEARS:
        path = INTERIM / f"fts_{fy}_facts.csv"
        if not path.exists():
            sys.exit(f"Missing interim facts: {path.relative_to(ROOT)} — run the extractors first")
        frames.append(pd.read_csv(path, dtype={"hs8": str, "country_code": str}))
    df = pd.concat(frames, ignore_index=True)
    # unit is legitimately nullable; keep NaN as a real "no unit" value, not "NaN" text.
    return df


def normalize_country_code(df):
    """Zero-pad numeric country codes to 3 digits — the country-code twin of hs8 zfill.

    The Excel extractor (2024-25) left country codes unpadded ("16" = CANADA) while
    the PDF extractor padded them ("016"). Left alone, dim_country would hold Canada
    twice and split its facts across two keys, silently breaking any per-destination
    trend that crosses the Excel/PDF boundary (analytical question 3). Padding
    conforms them. Non-numeric codes (name-keyed countries, UNKNOWN) pass through
    untouched. Safe on the grain: within a year no two distinct codes collapse to the
    same 3 digits, so no new duplicate keys are created.
    """
    df = df.copy()
    df["country_code"] = df["country_code"].map(
        lambda c: c.zfill(3) if isinstance(c, str) and ANY_DIGITS.match(c) else c)
    return df


def load_hs_desc():
    """Union the per-year hs8 -> (unit, description) side files into one label table.

    Where a code appears in several years we keep the LATEST year's label (spellings
    and units are most current there). Returns a frame indexed for easy lookup.
    """
    frames = []
    for fy in FISCAL_YEARS:
        path = INTERIM / f"fts_{fy}_hs_desc.csv"
        if not path.exists():
            sys.exit(f"Missing hs descriptions: {path.relative_to(ROOT)} — re-run the extractors")
        d = pd.read_csv(path, dtype={"hs8": str})
        d["fy"] = fy
        frames.append(d)
    alld = pd.concat(frames, ignore_index=True)
    # latest-year wins: sort by fy then drop_duplicates keeping last.
    alld = alld.sort_values("fy").drop_duplicates("hs8", keep="last")
    return alld.set_index("hs8")


# ---------------------------------------------------------------------------
# Aggregate to the grain
# ---------------------------------------------------------------------------

def aggregate_to_grain(raw):
    """Collapse finer-than-HS8 source line items onto the declared grain (SUM).

    DATA_ISSUES #1: some source rows are finer than HS8 (sub-codes / repeated
    commodity sections) and share a grain key. They are legitimate (each carries its
    own quantity+value; totals reconcile), so we AGGREGATE — never de-duplicate/drop,
    which would delete real trade value. dropna=False keeps null-unit groups (unit is
    a nullable grain column). Aggregation here is lossless: the rows collapsing into
    one key all share the same unit, so summing quantity is meaningful.
    """
    facts = (raw.groupby(GRAIN, dropna=False, as_index=False)[MEASURES].sum())
    return facts


def assert_conservation(raw, facts):
    """Prove aggregation moved no value: per (fy, flow), pre-sum == post-sum.

    This is the whole safety argument for aggregating instead of dropping — the total
    is invariant. rtol is tiny (1e-9): a genuine bug (a dropped row) shifts a sum far
    more than float re-summation noise ever could.
    """
    problems = []
    for (fy, flow), g in raw.groupby(["fy", "flow"]):
        f = facts[(facts["fy"] == fy) & (facts["flow"] == flow)]
        for m in MEASURES:
            pre, post = g[m].sum(), f[m].sum()
            if not np.isclose(pre, post, rtol=1e-9, atol=1.0):
                problems.append(f"{fy}/{flow} {m}: pre {pre:,.2f} != post {post:,.2f}")
    if problems:
        sys.exit("CONSERVATION FAILED (aggregation lost value):\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------------------
# dim_country  (+ surrogate keys for name-keyed countries, + remap facts)
# ---------------------------------------------------------------------------

def build_dim_country(raw):
    """Authoritative country dimension, and a map to remap name-keyed facts.

    Three kinds of country_code arrive from extraction:
      - a 3-digit BBS code (the normal case),
      - a name (BBS printed no numeric code, e.g. WEST.SAHARA — extraction keyed it by
        its own name), and
      - the UNKNOWN sentinel (2023-24 exports lost a destination label; value kept).

    We give every name-keyed country a STABLE SURROGATE KEY (U01, U02, ... assigned in
    sorted-name order; the alpha prefix can never collide with a numeric BBS code) so
    the fact's foreign key is always a short code, and we remap those facts. The
    authoritative display name for each code is taken from the LATEST fiscal year the
    code appears in — that resolves spelling drift across years (e.g. "UNTD ARAB EM").

    Returns (dim_country, remap) where remap maps an original name-key -> surrogate.
    """
    cc = raw[["country_code", "country_name", "fy"]].copy()

    # authoritative name per code = its name in the latest fy where it HAS a name.
    # (last NON-NULL, not last row: a 2023-24 label loss must not blank a code that
    # was named in earlier years; and 2024-25's correct labels override the 2023-24
    # "TUVALU" export mis-read, since 2024-25 is later.)
    named_rows = cc.dropna(subset=["country_name"]).sort_values("fy")
    latest = (named_rows.drop_duplicates("country_code", keep="last")
              .set_index("country_code")["country_name"])

    def name_for(code):
        # Fallback for the handful of 2023-24 codes whose label was lost entirely:
        # keep the real BBS code, flag the lost name — never emit a null dim label.
        return latest[code] if code in latest.index else f"Unknown ({code})"

    all_codes = sorted(cc["country_code"].unique())
    numeric = [c for c in all_codes if NUMERIC_CC.match(c)]
    named = [c for c in all_codes if not NUMERIC_CC.match(c) and c != UNKNOWN_CC]

    # surrogate codes for name-keyed countries, deterministic (sorted-name order).
    remap = {name: f"U{i:02d}" for i, name in enumerate(sorted(named), start=1)}

    rows = []
    for c in numeric:
        rows.append((c, name_for(c), False))
    for name in sorted(named):
        rows.append((remap[name], name_for(name), True))
    if (raw["country_code"] == UNKNOWN_CC).any():
        rows.append((UNKNOWN_CC, "Unknown destination", False))

    dim = pd.DataFrame(rows, columns=["country_code", "country_name", "is_surrogate"])
    return dim.sort_values("country_code").reset_index(drop=True), remap


def remap_country_codes(df, remap):
    """Point name-keyed facts at their surrogate code (numeric/UNKNOWN untouched)."""
    if remap:
        df["country_code"] = df["country_code"].map(lambda c: remap.get(c, c))
    return df


# ---------------------------------------------------------------------------
# dim_hs
# ---------------------------------------------------------------------------

def build_dim_hs(facts, hs_desc, chapters):
    """Product dimension: every hs8 that appears in facts, with rollup + labels.

    Keys off the FACTS (every product that has trade must resolve), left-joined to the
    harvested BBS labels and the HS chapter names. hs4/hs2 are pure substrings of the
    zero-padded hs8 — the rollup the reconciliation already leaned on. A code with no
    harvested description keeps a null label (counted in QC), never gets dropped.
    """
    codes = pd.DataFrame({"hs8": sorted(facts["hs8"].unique())})
    codes["hs4"] = codes["hs8"].str[:4]
    codes["hs2"] = codes["hs8"].str[:2]

    dim = codes.merge(hs_desc[["unit", "description"]], on="hs8", how="left")
    dim = dim.merge(chapters, on="hs2", how="left")
    return dim[["hs8", "hs4", "hs2", "description", "unit", "chapter_name"]]


# ---------------------------------------------------------------------------
# dim_fx_rate
# ---------------------------------------------------------------------------

def load_dim_fx_rate():
    """Read the curated Bangladesh Bank period-average rates (checked-in reference)."""
    path = REFERENCE / "fx_bdt_usd.csv"
    if not path.exists():
        sys.exit(f"Missing FX reference: {path.relative_to(ROOT)} — see plan step 3")
    fx = pd.read_csv(path)
    return fx


def load_chapters():
    path = REFERENCE / "hs_chapters.csv"
    if not path.exists():
        sys.exit(f"Missing chapter reference: {path.relative_to(ROOT)} — see plan step 3")
    return pd.read_csv(path, dtype={"hs2": str})


# ---------------------------------------------------------------------------
# Write Parquet
# ---------------------------------------------------------------------------

def write_facts(facts):
    """Write facts as Parquet partitioned by flow + year.

    'fy' is renamed to 'year' purely so the partition directory reads `year=2020-21`
    (Athena convention). flow and year become PARTITION columns: pandas writes them as
    directory names and DROPS them from the file body, so the on-disk columns are
    exactly half, hs8, country_code, unit, quantity, value_bdt.
    """
    out = facts.rename(columns={"fy": "year"}).copy()
    if TRADE_DIR.exists():
        import shutil
        shutil.rmtree(TRADE_DIR)   # idempotent rebuild (safe: derived data only)
    out.to_parquet(TRADE_DIR, partition_cols=["flow", "year"], index=False)


def write_dim(dim, name):
    d = DIMS_DIR / name
    if d.exists():
        import shutil
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    dim.to_parquet(d / f"{name}.parquet", index=False)


# ---------------------------------------------------------------------------
# QC verdict
# ---------------------------------------------------------------------------

def qc_report(raw, facts, dim_hs, dim_country, dim_fx):
    """Compute the M3 quality gate: conservation, grain, FK integrity, residuals.

    Returns (lines, ok). `lines` is markdown for docs/QC_M3.md and stdout.
    """
    lines, ok = [], True

    def check(label, passed, detail=""):
        nonlocal ok
        ok &= passed
        mark = "PASS" if passed else "FAIL"
        lines.append(f"- **{mark}** — {label}" + (f": {detail}" if detail else ""))
        return passed

    lines.append("# M3 QC Verdict — dimensional model\n")
    lines.append(f"Rows: interim {len(raw):,} -> fact_trade {len(facts):,} "
                 f"(collapsed {len(raw) - len(facts):,} finer-than-HS8 line items).\n")

    # 1. grain uniqueness
    dup = int(facts.duplicated(GRAIN).sum())
    check("grain is unique (fact_trade key)", dup == 0, f"{dup} duplicate keys")

    # 2. conservation per year-flow (value + quantity)
    cons_bad = []
    for (fy, flow), g in raw.groupby(["fy", "flow"]):
        f = facts[(facts["fy"] == fy) & (facts["flow"] == flow)]
        for m in MEASURES:
            if not np.isclose(g[m].sum(), f[m].sum(), rtol=1e-9, atol=1.0):
                cons_bad.append(f"{fy}/{flow}/{m}")
    check("value & quantity conserved vs interim (per year-flow)", not cons_bad,
          ", ".join(cons_bad) if cons_bad else "all 10 year-flows exact")

    # 3. FK integrity: every fact key resolves in its dimension
    hs_orphans = int((~facts["hs8"].isin(set(dim_hs["hs8"]))).sum())
    check("every fact hs8 resolves in dim_hs", hs_orphans == 0, f"{hs_orphans} orphans")

    cc_orphans = int((~facts["country_code"].isin(set(dim_country["country_code"]))).sum())
    check("every fact country_code resolves in dim_country", cc_orphans == 0,
          f"{cc_orphans} orphans")

    fx_keys = set(map(tuple, dim_fx[["fy", "half"]].values))
    fact_keys = set(map(tuple, facts[["fy", "half"]].drop_duplicates().values))
    fx_missing = fact_keys - fx_keys
    check("every (fy, half) in facts has an FX rate", not fx_missing,
          f"missing {sorted(fx_missing)}" if fx_missing else "all 10 present")

    # 4. non-negative measures
    neg = int((facts["quantity"] < 0).sum() + (facts["value_bdt"] < 0).sum())
    check("no negative measures", neg == 0, f"{neg} negatives")

    # 5. informational (not gates) — restated residuals from DATA_ISSUES
    lines.append("")
    lines.append("## Informational (carried to M5 as view-layer footnotes)\n")
    missing_desc = int(dim_hs["description"].isna().sum())
    lines.append(f"- dim_hs products without a harvested label: {missing_desc} "
                 f"of {len(dim_hs):,} (kept, null description)")
    missing_ch = int(dim_hs["chapter_name"].isna().sum())
    unmatched_hs2 = sorted(dim_hs.loc[dim_hs["chapter_name"].isna(), "hs2"].unique())
    lines.append(f"- dim_hs rows without a chapter name: {missing_ch} "
                 f"(hs2 not in reference: {unmatched_hs2})")
    surro = int(dim_country["is_surrogate"].sum())
    lines.append(f"- name-keyed countries given surrogate codes: {surro}")
    unk = int((facts["country_code"] == UNKNOWN_CC).sum())
    unk_val = facts.loc[facts["country_code"] == UNKNOWN_CC, "value_bdt"].sum()
    lines.append(f"- UNKNOWN-destination fact rows: {unk} "
                 f"(BDT {unk_val:,.0f}; exclude from per-destination views)")
    exp = facts[facts["flow"] == "X"]
    usable = exp[(exp["quantity"] > 0) & exp["unit"].notna()]
    lines.append(f"- export rows usable for unit price (qty>0 & unit set): "
                 f"{len(usable):,}/{len(exp):,} ({len(usable)/len(exp)*100:.1f}%)")

    lines.append("")
    lines.append(f"## VERDICT: {'PASS' if ok else 'FAIL'}\n")
    return lines, ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    print("Loading interim facts + harvested labels + reference seeds ...")
    raw = load_interim()
    raw = normalize_country_code(raw)   # conform 2024-25 unpadded codes before anything keys off them
    hs_desc = load_hs_desc()
    chapters = load_chapters()
    dim_fx = load_dim_fx_rate()

    print("Aggregating to the declared grain ...")
    facts = aggregate_to_grain(raw)
    assert_conservation(raw, facts)

    print("Building dimensions ...")
    dim_country, remap = build_dim_country(raw)
    facts = remap_country_codes(facts, remap)
    dim_hs = build_dim_hs(facts, hs_desc, chapters)

    print("Writing Parquet (facts partitioned by flow/year; dims one folder each) ...")
    facts_out = facts.drop(columns=[])  # country_name already absent (grain has none)
    write_facts(facts_out)
    write_dim(dim_hs, "dim_hs")
    write_dim(dim_country, "dim_country")
    write_dim(dim_fx, "dim_fx_rate")

    lines, ok = qc_report(raw, facts, dim_hs, dim_country, dim_fx)
    QC_DOC.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nWrote QC verdict -> {QC_DOC.relative_to(ROOT)}")
    print(f"OVERALL: {'PASS' if ok else 'FAIL — do not proceed to M4'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
