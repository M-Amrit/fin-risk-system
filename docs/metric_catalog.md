# Phase 7 — Enterprise metric definition catalog

Sign convention: all P&L amounts are **natural sign** (revenue and expense positive). "Source" names objects that exist
in `sql/` and were executed. Corrections to the original brief are marked **[fixed]**.

| Metric | Formula | Lineage | Interpretation / caveats |
|---|---|---|---|
| **Gross Revenue** | Σ signed(acct type = Revenue) = interest income (4000) + fee income (4100) **[fixed: the brief said "credit sales"; a lender has none]** | `mv_gl_monthly` → `v_pl_monthly.gross_revenue` | Top line before funding cost |
| **Net Revenue** | Gross Revenue − Interest Expense (5000) | `v_pl_monthly.net_revenue` | Bank-style (NII + fees). Other firms define "net revenue" as revenue net of returns; this one does not |
| **Operating Income** | Net Revenue − servicing (5100) − operating expense (6xxx) | `v_pl_monthly.operating_income` | **Before** credit provisions |
| **Pre-tax / Net Income** | Operating Income − Provision (7000) | `v_pl_monthly.pre_tax_income` | No tax accounts exist, so pre-tax = net. Never present as post-tax |
| **Net Interest Margin** | 12 × (Interest income − Interest expense) ÷ avg(gross loans open, close) × 100 | `v_nim_monthly` | Earning assets = acct 1200 only. Annualised from one month, so noisy |
| **Variance (favourable +)** | Revenue: Actual − Budget; Expense: Budget − Actual | `v_variance_analysis.favourable_variance`; DAX `[Budget Variance]` | Direction-aware; positive always good |
| **Variance %** | Favourable Variance ÷ \|Budget\| × 100 (NULL if Budget = 0) | `variance_pct` (raw Actual−Budget basis), DAX `[Variance %]` (favourable basis) **[note]** | Two bases exist: SQL `variance_pct` is raw (Actual−Budget)/Budget as in the brief; DAX/KPI feed use favourable ÷ \|Budget\|. Don't mix them on one page |
| **Probability of Default (PD)** | PD = 1 ÷ (1 + e^(−z)), z = β₀ + Σ βᵢ·WoEᵢ; in production PD is derived from the integer score: PD = 1 ÷ (1 + exp((Score − Offset)/Factor)) | `scorecard_model.joblib`, `stg_scored_loans.pd` **[fixed: brief cited `fact_borrower`, which doesn't exist; features come from `dim_borrower`]** | **Not a 12-month PD.** Target = eventual charge-off on resolved loans (lifetime-style). Using it as 12-month PD in provisioning overstates EL |
| **Score** | Offset + Factor·ln(good:bad odds); Factor = PDO/ln 2 = 28.854; Offset = 600 − Factor·ln 50 = 487.123 | `scorecard_points.csv` | 600 ⇔ 50:1, +20 points doubles odds. Verified by test |
| **Exposure at Default (EAD)** | Outstanding principal (+ undrawn × CCF for revolving lines) | `fact_credit_performance.outstanding_principal` | Instalment loans have no undrawn commitment, so CCF term = 0 here. Does not include accrued interest |
| **Loss Given Default (LGD)** | 1 − Σ recoveries ÷ Σ loan_amnt on defaulted training loans | `stg_scored_loans.lgd` | Proxy: denominator is original amount, not balance at default, so LGD is overstated (conservative). Single portfolio-wide number, not by segment |
| **Expected Loss (EL)** | PD × LGD × EAD, summed per risk band | `fact_provisioning_ledger.expected_loss_amount` | Not an accounting provision by itself. IFRS 9 needs 12-month/lifetime ECL with staging; CECL needs lifetime losses. See limitations |
| **Allowance coverage** | Allowance (1210) ÷ EAD | `v_provision_recon.booked_coverage_pct` | Sample data: booked 4.6% vs modelled 13.6% — a 3× gap |
| **PSI** | Σ (%actualᵢ − %expectedᵢ)·ln(%actualᵢ ÷ %expectedᵢ), bins from expected, empty bins floored at 1e-4 | `characteristic_psi.csv`, `metrics.json` | Rule of thumb: < 0.10 stable, 0.10–0.25 monitor, > 0.25 investigate. It detects drift, not model decay; decay needs outcomes |
| **AUC / Gini / KS** | Gini = 2·AUC − 1; KS = max(TPR − FPR) | `metrics.json` | Computed out-of-time on test; cutoff chosen on train |
| **IV** | Σ (%good − %bad)·WoE | `iv_table.csv` | < 0.02 useless, 0.02–0.1 weak, 0.1–0.3 medium, 0.3–0.5 strong, > 0.5 suspicious (heuristics) |
