# Usage: make all PGDATABASE=finrisk      (requires psql access; run from repo root)
PG ?= psql -d $(PGDATABASE) -v ON_ERROR_STOP=1 -q
.PHONY: all scorecard ledger db test clean
all: scorecard ledger db test

scorecard:
	cd python_scorecard && python run_scorecard.py --synthetic 200000 --out ../data/scorecard_out --export-star ../data/synthetic

ledger:   # anchors the GL loan book to the scored EAD (link by construction)
	cd python_scorecard && python generate_ledger.py --out ../data/synthetic --start 2019-01 --months 36 \
	  --anchor-ead $$(python -c "import pandas as pd;print(pd.read_csv('../data/scorecard_out/stg_scored_loans.csv').ead.sum())")

db:
	$(PG) -c "DROP SCHEMA IF EXISTS fin CASCADE"
	$(PG) -f sql/01_ddl.sql
	$(PG) -f sql/00_load_synthetic.sql
	$(PG) -f sql/00b_load_loans.sql
	$(PG) -f sql/02_etl_transformations.sql
	$(PG) -f sql/03_data_quality_audits.sql
	$(PG) -f sql/04_board_kpis.sql

test:
	python -m pytest tests -q

clean:
	rm -rf data/scorecard_out data/synthetic/*.csv
