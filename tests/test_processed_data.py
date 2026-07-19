"""
Contract tests over the PROCESSED dimensional model (data/processed/).

WHY this layer exists (vs the interim contract in test_extracted_data.py)
------------------------------------------------------------------------
test_extracted_data.py freezes what the interim CSVs promise. This file freezes what
the STAR SCHEMA promises to M4 (Glue/Athena) and M5 (analysis SQL): the fact grain is
unique, no value was lost turning interim -> facts, every fact key resolves in its
dimension (referential integrity), the partition layout Athena expects is on disk, and
the partition/denormalised columns really were removed from the fact files. If a future
re-run of transform/build_warehouse.py silently changes the model, a test trips here
instead of a dashboard going wrong three milestones later.

These tests read the interim CSVs too, and assert conservation BETWEEN the two layers —
that is the safety proof for aggregating (not dropping) the finer-than-HS8 line items.

Run:    ./.venv/bin/python -m pytest tests/test_processed_data.py -v
(Processed data is git-ignored; the whole module skips if it hasn't been built.)
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
INTERIM = ROOT / "data" / "interim"
TRADE = ROOT / "data" / "processed" / "trade"
DIMS = ROOT / "data" / "processed" / "dims"

FISCAL_YEARS = ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25"]
FLOWS = ["X", "M"]

# The fact grain, as it reads back from the partitioned dataset (flow + year are
# restored from the directory names; 'year' is the interim 'fy').
GRAIN = ["flow", "year", "half", "hs8", "country_code", "unit"]
# Exactly what a fact FILE body may contain — partition cols (flow, year) and the
# denormalised country_name must NOT be here.
FACT_FILE_COLUMNS = ["half", "hs8", "country_code", "unit", "quantity", "value_bdt"]


def _require(path):
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} not built — run transform/build_warehouse.py")


@pytest.fixture(scope="module")
def facts():
    _require(TRADE)
    # Reading the dataset root restores flow/year as columns from the partition dirs.
    df = pd.read_parquet(TRADE)
    return df


@pytest.fixture(scope="module")
def interim():
    frames = []
    for fy in FISCAL_YEARS:
        p = INTERIM / f"fts_{fy}_facts.csv"
        _require(p)
        frames.append(pd.read_csv(p, dtype={"hs8": str, "country_code": str}))
    return pd.concat(frames, ignore_index=True)


@pytest.fixture(scope="module")
def dim_hs():
    _require(DIMS / "dim_hs")
    return pd.read_parquet(DIMS / "dim_hs")


@pytest.fixture(scope="module")
def dim_country():
    _require(DIMS / "dim_country")
    return pd.read_parquet(DIMS / "dim_country")


@pytest.fixture(scope="module")
def dim_fx():
    _require(DIMS / "dim_fx_rate")
    return pd.read_parquet(DIMS / "dim_fx_rate")


class TestPartitionLayout:
    def test_all_ten_partitions_exist(self):
        _require(TRADE)
        for flow in FLOWS:
            for fy in FISCAL_YEARS:
                d = TRADE / f"flow={flow}" / f"year={fy}"
                assert d.is_dir(), f"missing partition {d.relative_to(ROOT)}"

    def test_fact_files_exclude_partition_and_denormalised_columns(self):
        # Read ONE leaf file directly (not via the dataset reader, which re-adds the
        # partition columns): flow/year live only in the path, and country_name was
        # dropped to dim_country. The file body must be exactly FACT_FILE_COLUMNS.
        _require(TRADE)
        leaf = next(TRADE.glob("flow=*/year=*/*.parquet"))
        cols = list(pd.read_parquet(leaf).columns)
        assert cols == FACT_FILE_COLUMNS, f"unexpected fact-file columns: {cols}"


class TestGrain:
    def test_grain_is_unique(self, facts):
        dup = int(facts.duplicated(GRAIN).sum())
        assert dup == 0, f"{dup} duplicate grain keys — aggregation did not make the key unique"

    def test_hs8_still_eight_digit_strings(self, facts):
        # Parquet must preserve the zero-padding; an int round-trip would drop it.
        bad = facts[~facts["hs8"].str.fullmatch(r"\d{8}")]
        assert bad.empty, f"non-8-digit hs8: {bad['hs8'].unique()[:10]}"


class TestConservation:
    def test_value_and_quantity_conserved_from_interim(self, facts, interim):
        # The whole argument for aggregating (not dropping) sub-HS8 rows: the totals
        # per year-flow are invariant. Compare processed (year) to interim (fy).
        # observed=True: year/flow read back from parquet partitions as categoricals.
        f = facts.groupby(["year", "flow"], observed=True)[["quantity", "value_bdt"]].sum()
        i = interim.groupby(["fy", "flow"])[["quantity", "value_bdt"]].sum()
        i.index = i.index.set_names(["year", "flow"])
        assert set(f.index) == set(i.index), "year-flow partitions differ from interim"
        for key in f.index:
            for m in ["quantity", "value_bdt"]:
                assert np.isclose(f.loc[key, m], i.loc[key, m], rtol=1e-9, atol=1.0), (
                    f"{key} {m}: processed {f.loc[key, m]:,.2f} != interim {i.loc[key, m]:,.2f}")

    def test_row_count_dropped_only_by_collapsed_duplicates(self, facts, interim):
        # Rows may only DECREASE (finer-than-HS8 line items collapsing), never grow.
        assert len(facts) <= len(interim)


class TestReferentialIntegrity:
    def test_every_hs8_resolves_in_dim_hs(self, facts, dim_hs):
        orphans = set(facts["hs8"]) - set(dim_hs["hs8"])
        assert not orphans, f"hs8 in facts but not dim_hs: {sorted(orphans)[:10]}"

    def test_every_country_code_resolves_in_dim_country(self, facts, dim_country):
        orphans = set(facts["country_code"]) - set(dim_country["country_code"])
        assert not orphans, f"country_code in facts but not dim_country: {sorted(orphans)[:10]}"

    def test_every_fy_half_resolves_in_dim_fx(self, facts, dim_fx):
        fact_keys = set(map(tuple, facts[["year", "half"]].drop_duplicates().values))
        fx_keys = set(map(tuple, dim_fx[["fy", "half"]].values))
        assert fact_keys <= fx_keys, f"fy/half without an FX rate: {fact_keys - fx_keys}"


class TestDimensions:
    def test_dim_hs_key_unique_and_rollup_consistent(self, dim_hs):
        assert dim_hs["hs8"].is_unique
        # hs4/hs2 are prefixes of hs8 — the rollup the reconciliation leaned on.
        assert (dim_hs["hs4"] == dim_hs["hs8"].str[:4]).all()
        assert (dim_hs["hs2"] == dim_hs["hs8"].str[:2]).all()

    def test_dim_country_key_unique_and_named(self, dim_country):
        assert dim_country["country_code"].is_unique
        # No null display labels: lost names fall back to "Unknown (<code>)", never NaN.
        assert dim_country["country_name"].notna().all()

    def test_dim_fx_has_ten_rows_one_per_fiscal_half(self, dim_fx):
        assert len(dim_fx) == 10
        assert dim_fx[["fy", "half"]].drop_duplicates().shape[0] == 10
        # sanity band: the taka sat ~85 and fell toward ~122; nothing outside is plausible.
        assert dim_fx["bdt_per_usd"].between(80, 130).all()


class TestMeasures:
    def test_no_negative_measures(self, facts):
        assert (facts["quantity"] < 0).sum() == 0
        assert (facts["value_bdt"] < 0).sum() == 0
