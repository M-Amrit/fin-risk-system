# Phase 1 — Dataset evaluation and selection

Scores are **my judgement** on a 0–3 scale (3 = best), not measured benchmarks. Row/column counts are from the
public dataset documentation as I know it; confirm against your download. Licences/terms: check each source's page
(Kaggle competition data has acceptance rules; do not redistribute).

| # | Dataset | Fin. granularity (GL + loan) | Real-world messiness | Volume (>100k) | Hybrid feasibility | Verdict |
|---|---|---|---|---|---|---|
| 1 | LendingClub accepted loans 2007–2018 (~2.26M rows, ~150 cols) | Loan: 3 / GL: 0 | 3 | 3 | 2 | **Selected for credit module** |
| 2 | Home Credit Default Risk (application_train 307,511 × 122; ~8% default; 6 further relational tables) | Loan: 3 (has monthly DPD history) / GL: 0 | 3 | 3 | 2 | Runner-up; better target realism, heavier to join |
| 3 | Give Me Some Credit (150,000 × 11; ~6.7% default) | Loan: 1 / GL: 0 | 2 | 3 | 1 | Good teaching set; too few attributes |
| 4 | German Credit, UCI Statlog (1,000 × 20; 30% bad) | Loan: 1 / GL: 0 | 1 | 0 | 0 | Rejected: 1,000 rows |
| 5 | Taiwan credit-card default, UCI (30,000 × 23; ~22% default) | Loan: 2 (6 months repayment status) / GL: 0 | 2 | 0 | 1 | Rejected on volume |
| 6 | SEC EDGAR Financial Statement Data Sets (quarterly XBRL: sub/num/tag/pre) | Loan: 0 / GL: 1 (aggregates, not a ledger) | 3 | 3 | 1 | Rejected: no debits/credits, no budgets, no borrowers |
| 7 | Synthetic ledger (seeded) | GL: 3 (by design) | 0 (only what you inject) | 3 (choose) | 3 | **Selected for GL module** |

## Verdict: hybrid (LendingClub-shaped credit data + synthetic GL)

Why: no public dataset contains a general ledger with journals, a chart of accounts and budgets, and none that has
both retail credit attributes and corporate financials. So the GL **must** be synthetic.

Why LendingClub over Home Credit for the credit module: it has an **issue date**, enabling the out-of-time split that
credit modelling needs; it is one flat file; and its failure modes (leakage columns, right-censoring, no borrower id)
are the classic ones worth demonstrating. Home Credit is the better choice if you want a DPD-based target from
instalment history — swap `data.load_lendingclub` for a loader and keep everything downstream.

## What was actually run in this build (read this)

The build sandbox had no access to Kaggle. **All reported numbers come from a LendingClub-*shaped synthetic* loan book
(`data.synthetic_loans`, 200,000 loans)** generated from a logistic model with realistic missingness, outliers and
vintage drift. `data.load_lendingclub` is written against the published column names but **has not been executed on
real data**. Expect column-name or dtype fixes on first contact.

Consequences:
- Synthetic results demonstrate that the *code and controls* work (monotone bins, leakage caught, balanced ledger).
- They say **nothing** about achievable real-world Gini. On real LendingClub data without grade/int_rate, published work
  generally lands at a modest AUC (roughly mid-0.6s to ~0.7) — treat that as a prior, not a promise.
- The synthetic GL and the loan book are linked **by construction** (`--anchor-ead`), not by observation.

## Data-quality issues to expect on real LendingClub data (and how the code handles them)

| Issue | Handling |
|---|---|
| Post-origination fields (e.g. payments, recoveries, last_pymnt_*) | Never used as features; only `recoveries` used for LGD |
| `grade`, `sub_grade`, `int_rate` = LendingClub's own risk output | Excluded; demo flag shows they inflate Gini 0.27 → 0.42 (synthetic), and IV > 0.5 rule independently flags them |
| Right-censoring: recent loans have no outcome yet | `Current`/`Late`/`Grace` excluded from training; Current becomes the live book |
| Selection bias: only *accepted* applicants observed | Not corrected (no reject inference). Model is valid only for the accepted population |
| `emp_length` strings ("< 1 year", "10+ years"), `revol_util` with "%" | Parsed in loader |
| Missing values | Own WoE bin; if < 0.5% of rows, WoE = 0 |
| Footer rows in CSV | Filtered by numeric `id` |
| No borrower id | `borrower_id = loan_id` (repeat borrowers invisible) |
