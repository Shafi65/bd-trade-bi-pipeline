# One command per stage. Each stage exits non-zero on failure, and make stops at
# the first failure, so a reconciliation FAIL in `extract` halts the whole run
# before anything is modelled or deployed. Run targets in order (not with make -j).
#
#   make pipeline   extract + reconcile -> star schema -> tests   (local, no AWS)
#   make deploy     upload Parquet to S3, create Glue tables and Athena views
#   make stats      run the statistical tests against the Athena views
#   make all        pipeline + deploy + stats

PY ?= ./.venv/bin/python

# ATHENA_BUCKET / ATHENA_WORKGROUP come from the git-ignored .env (see .env.example)
-include .env
export

.PHONY: all pipeline extract warehouse test deploy stats

all: pipeline deploy stats

pipeline: extract warehouse test

extract:
	$(PY) ingestion/extract_fts_excel.py
	$(PY) ingestion/extract_fts_pdf.py

warehouse:
	$(PY) transform/build_warehouse.py

test:
	$(PY) -m pytest -q

deploy:
	@test -n "$(ATHENA_BUCKET)" || { echo "ATHENA_BUCKET is not set (copy .env.example to .env)"; exit 1; }
	aws s3 sync --delete data/processed s3://$(ATHENA_BUCKET)/processed
	$(PY) scripts/athena_run.py sql/create_external_tables.sql
	$(PY) scripts/athena_run.py sql/analysis_views.sql

stats:
	$(PY) analysis/stats_tests.py
