# Qlik Sense dashboard suite — specification & expression catalog

**Status:** designed against the executed PostgreSQL views and the generated scorecard CSVs. The Qlik app itself was
**not built or run** here (no Qlik engine in the build environment). Expression syntax follows Qlik set-analysis rules;
paste into a dev app and verify each KPI against the SQL views listed in the "Tie-out" column before publishing.

Load script: `load_script.qvs`. All amounts are **natural-sign** (`Amount`): revenue and expense are both positive.
Never `Sum(Amount)` across mixed account types — always filter by `AccountType` or use the pre-built expressions.

## Variables (created by the load script)

| Variable | Meaning |
|---|---|
| `vCurFY` | Fiscal year of the latest period that has GL postings |
| `vCurFP` | Latest closed fiscal period in that year |
| `vScorePSI`, `vTestGini`, `vTestKS`, `vTestAUC` | Scorecard monitoring scalars from `model_metrics_flat.csv` |

## Set-analysis expressions

### The three expressions requested in the brief (verbatim)

```qlik
// 1. Current Year YTD Revenue (as specified)
Sum({<FiscalYear={$(=Max(FiscalYear))}, AccountType={'Revenue'}>} Amount)

// 2. Prior Year YTD Revenue (SPLY) (as specified)
Sum({<FiscalYear={$(=Max(FiscalYear)-1)}, AccountType={'Revenue'}>} Amount)

// 3. High Risk Loan Portfolio Balance (as specified)
Sum({<RiskBand={'High Risk', 'Very High Risk'}>} LoanBalance)
```

**Flaw in #1 and #2 as written (do not ship them unmodified).** They filter the *year* but not the *period*:

- #1 equals true YTD only if the GL table contains no periods beyond the latest closed one. True today; breaks the day a
  forecast or a partially-posted month is loaded.
- #2 is **not** like-for-like. It sums the *whole* prior year, so while the current year is in month 6, SPLY is a
  12-month figure compared against a 6-month figure. Every YoY growth number built on it is wrong (large negative).
- `Max(FiscalYear)` also inherits whatever the user has selected; with a selection on `FiscalYear` it returns the max
  of the *selected* years.

### Hardened versions (use these)

```qlik
// 1'. Current Year YTD Revenue — ignores user selection on year/period, capped at latest closed period
Sum({1<FiscalYear={$(vCurFY)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Revenue'}>} Amount)

// 2'. Prior Year YTD Revenue — SAME period window as the current year (true like-for-like)
Sum({1<FiscalYear={$(=$(vCurFY)-1)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Revenue'}>} Amount)

// 2''. YoY growth %
(Sum({1<FiscalYear={$(vCurFY)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Revenue'}>} Amount)
 / Sum({1<FiscalYear={$(=$(vCurFY)-1)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Revenue'}>} Amount)) - 1
```

`{1<...>}` ignores all user selections (use it for headline KPIs that must not move). Use `{$<...>}` where you
*want* the KPI to respond to slicers, but then you accept that YTD semantics follow the user's selection.

### Sheet 1 — Executive C-Suite Overview

| KPI | Expression | Tie-out |
|---|---|---|
| Net Revenue (YTD) | `Sum({1<FiscalYear={$(vCurFY)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Revenue'}>} Amount) - Sum({1<FiscalYear={$(vCurFY)}, FiscalPeriod={"<=$(vCurFP)"}, AccountCode={'5000'}>} Amount)` | `v_pl_monthly.net_revenue` summed over YTD |
| Operating Income (YTD) | `[Net Revenue (YTD)] - Sum({1<FiscalYear={$(vCurFY)}, FiscalPeriod={"<=$(vCurFP)"}, AccountType={'Expense'}, AccountCode-={'5000','7000'}>} Amount)` | `v_pl_monthly.operating_income` |
| Total Loan Portfolio Value | `Sum({1<ScoreDate={"$(=Max(ScoreDate))"}>} LoanBalance)` | `fact_provisioning_ledger.ead_total` latest |
| Avg Portfolio Credit Score (EAD-weighted) | `Sum({1} Score*LoanBalance) / Sum({1} LoanBalance)` | mean of `stg_scored_loans.score` weighted by `ead` |
| Default Rate (%) — resolved loans only | `Count({1<LoanStatus={'Charged Off','Default'}>} LoanID) / Count({1<LoanStatus={'Charged Off','Default','Fully Paid'}>} LoanID)` | `fact_loan_portfolio` |
| Expected Loss | `Sum({1} ExpectedLoss)` | `v_board_kpis.expected_loss` |

Default Rate counts only **resolved** loans; including `Current` loans in the denominator understates the rate and
makes it fall mechanically as the book grows.

Visuals:
- **P&L waterfall:** dimension `PLLine` (Gross Revenue, Interest Expense, Servicing, Opex, Provision, Pre-Tax Income);
  measure = signed contribution. Use the native Waterfall chart where your release has it; otherwise the Visualization bundle.
  Build `PLLine` signs in the load script (expenses negative) — do not rely on chart-side sign flipping.
- **Revenue vs Budget with 12-month moving average:** line chart on `PeriodEnd`;
  measure 1 `Sum({1<AccountType={'Revenue'}>} Amount)`; measure 2 `Sum({1<AccountType={'Revenue'}>} Amount)` budget twin
  from the `Variance` table `Sum({1} BudgetAmt)` filtered to revenue accounts; MA line:
  `RangeAvg(Above(Sum({1<AccountType={'Revenue'}>} Amount), 0, 12))` — **first 11 points are partial averages**;
  hide them or label "partial window".
- **Risk band distribution:** bar, dimension `RiskBand` (sort by custom order Very Low → Very High),
  measure `Sum(LoanBalance)` and second axis `Count(LoanID)`.

### Sheet 2 — Financial Performance & Variance Drilldown

- **Pivot table:** drill-down dimension `Category > SubCategoryLevel > AccountName`; measures `Actual`, `Budget`,
  `Favourable variance`, `Variance %`:
  - Actual: `Sum({<AccountType={'Revenue','Expense'}>} Amount)`
  - Budget: `Sum(BudgetAmt)`
  - Favourable variance: `Sum(FavVariance)` (already direction-adjusted in SQL: + = good)
  - Variance %: `Sum(FavVariance) / Sum(Fabs(BudgetAmt))` → wrap in `If(Sum(Fabs(BudgetAmt))=0, Null(), …)`
- **Variance heatmap by department:** matrix — dimensions `Department` × `FiscalPeriod`, measure `Sum(FavVariance)`,
  diverging colour with **0 as the fixed midpoint** (green favourable / red unfavourable). A sequential scale here
  misleads.
- **Driver scatter (expense variance vs volume driver):** dimension `AccountName`; X = loan volume driver
  `Avg(TOTAL <PeriodKey> LoanBalance)` is *not* available per period from the current model — the GL has no
  transaction-count driver. Implement with the SQL-side driver table (monthly average gross loans from account 1200
  balances, available in `v_balance_sheet_snapshot`); Y = `Sum(FavVariance)` for expense accounts. Without that driver table
  the chart has nothing to plot.

### Sheet 3 — Credit Risk Scorecard Analytics

| Visual | Source | Notes |
|---|---|---|
| PSI gauge | `$(vScorePSI)` | Bands: green < 0.10, amber 0.10–0.25, red ≥ 0.25. Rules of thumb, not statistical tests. |
| WoE trend | `WoETrend`: x = `BinOrder`, y = `WoE`, dimension `Feature` | Must be monotone for numeric features; a break means a binning/data issue. |
| Score distribution histogram | `Loans.Score` | Bin width 10 points; overlay the training distribution if loaded. |
| Risk-band bad-rate matrix | `BandSummary`: rows `RiskBand`, columns `Sample` (train/test), values `ObservedPD`, `PredictedPD` | Observed ≫ predicted on test = model under-forecasting. |
| Loss provisioning forecast | scenario, see below | |

**Loss provisioning forecast.** The model gives point-in-time EL; it has no macro-economic forecasting component. The
"forecast" visual is a *scenario* calculator: `Sum(ExpectedLoss) * vStressMultiplier` where `vStressMultiplier` is a
variable-input slider (1.0 = base, 1.5 = +50% PD stress). Label it a sensitivity, never a forecast. A real forecast
requires a lifetime-PD term structure and macro overlays that this build does not contain.

### Sheet 1/2/3 shared

- `Variance` table joins on `PeriodKey`, `AccountCode`, `DepartmentID`; the GL table shares the same keys — Qlik will
  create a synthetic key across `GL` and `Variance` (three common fields). Break it by creating one composite key
  `PeriodKey & '|' & AccountCode & '|' & DepartmentID as %VarKey` in both tables if the data model viewer shows `$Syn`.
- Master items: create each KPI above as a Master Measure so Sheets 1–3 share one definition.
