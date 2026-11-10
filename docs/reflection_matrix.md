# Individual ownership & reflection matrix

**Owner** is Amrit for every row, as instructed. If teammates contributed to a component, add their names; this file does not
know who else worked on what. The other columns are filled from what was actually built and tested; edit them to reflect your
own experience where it differs.

| Component | Owner | Core challenge overcome | Technical learning | Business value delivered | Evidence |
|---|---|---|---|---|---|
| Dataset evaluation & schema | Amrit | No public dataset has both a GL and credit data, so a hybrid was needed; spec's `journal_id` could not test journal balance | Trade-offs between 7 candidate datasets; adding `entry_batch_id` and `normal_balance` | A defensible data choice and schema that supports double-entry checks | `docs/dataset_evaluation.md`, `sql/01_ddl.sql` |
| GL generator | Amrit | Audit found 30 lines posted outside their month (February); fixed by clamping to month end | Double-entry invariants; seeded, reproducible synthetic data | Reproducible test ledger, scalable to 312k lines | `generate_ledger.py`, ledger test |
| SQL ETL & audits | Amrit | Circular view dependencies (KPI view needs audit view); balance sheet needed a calendar spine for quiet months | Window functions, FULL OUTER JOIN variance, materialised views, defect-injection tests | 18 checks that tie P&L, balance sheet and budget to the cent | `sql/02-04`, `tests/test_sql_audits.py` |
| Power Pivot / DAX | Amrit | Revenue and expense variance have opposite good/bad directions and cannot be netted | Context transition, direction-aware variance, YTD | Self-service favourable/unfavourable variance | `power_pivot/measures.dax` |
| Scorecard engine | Amrit | Leakage (`int_rate`, `grade`: Gini 0.27 inflated to 0.42), non-monotone bins, WoE sign convention vs coefficient sign | WoE/IV, sign-constrained logistic regression, score scaling (600 at 50:1, PDO 20), PSI | Ranked risk bands and an expected-loss feed to the ledger | `woe.py`, `model.py`, `run_scorecard.py` |
| Qlik dashboards | Amrit | The brief's YTD/SPLY expressions compare a partial year with a full year | Set-analysis pitfalls; fixed vs selection-driven KPIs | Executive view with consistent definitions | `qlik_dashboards/dashboard_spec.md` |
| Power Automate | Amrit | Stale risk data (33 months) could reach the board; day-1 send precedes month-end close | Gating logic, failure scopes, connector licensing limits | Controlled, self-checking monthly delivery | `power_automate/flow_spec.md` |
| Testing / QA | Amrit | Passing tests can be vacuous | Mutation-checking tests; tests that inject defects | 23 tests guarding regressions | `tests/` |
