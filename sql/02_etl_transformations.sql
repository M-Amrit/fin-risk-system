-- =====================================================================================
-- 02_etl_transformations.sql | P&L pipeline, Balance Sheet snapshot, Variance, Provision bridge
-- Run after 01_ddl.sql and data load. Refresh with: REFRESH MATERIALIZED VIEW CONCURRENTLY fin.mv_gl_monthly;
-- =====================================================================================
SET search_path = fin, public;

-- ---------- 0. Populate dim_date from the ledger's own range (idempotent) -----------
INSERT INTO dim_date (date_key, year, month, month_start, fiscal_year, fiscal_period, year_month)
SELECT d::date, EXTRACT(YEAR FROM d)::smallint, EXTRACT(MONTH FROM d)::smallint,
       date_trunc('month', d)::date, EXTRACT(YEAR FROM d)::smallint, EXTRACT(MONTH FROM d)::smallint,
       to_char(d, 'YYYY-MM')
FROM generate_series(
        (SELECT date_trunc('year', MIN(posting_period)) FROM fact_general_ledger),
        (SELECT date_trunc('year', MAX(posting_period)) + INTERVAL '1 year' - INTERVAL '1 day' FROM fact_general_ledger),
        INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;

-- ---------- 1. Monthly pre-aggregation (the performance lever at >100k GL lines) -----
DROP MATERIALIZED VIEW IF EXISTS mv_gl_monthly CASCADE;
CREATE MATERIALIZED VIEW mv_gl_monthly AS
SELECT  posting_period, account_id, account_code, account_type, financial_statement, sub_category,
        normal_balance, department_id,
        SUM(debit_amount)  AS debit_amount,
        SUM(credit_amount) AS credit_amount,
        SUM(net_dr_cr)     AS net_dr_cr,
        SUM(signed_amount) AS signed_amount,
        COUNT(*)           AS line_count
FROM v_gl_signed
GROUP BY posting_period, account_id, account_code, account_type, financial_statement, sub_category,
         normal_balance, department_id;
CREATE UNIQUE INDEX ux_mv_gl_monthly ON mv_gl_monthly (posting_period, account_id, department_id);
CREATE INDEX ix_mv_gl_monthly_type ON mv_gl_monthly (account_type, posting_period);

-- ---------- 2. P&L PIPELINE ---------------------------------------------------------
-- Definitions (documented in docs/metric_catalog.md):
--   gross_revenue   = all Revenue accounts (interest income + fee income)
--   net_revenue     = gross_revenue - interest expense (acct 5000)        [bank-style: NII + fees]
--   operating_income= net_revenue - other COGS (servicing) - operating expense   (BEFORE provisions)
--   pre_tax_income  = operating_income - provision for credit losses
--   No tax accounts exist in this COA, so pre_tax_income IS net income. Do not present it as post-tax.
CREATE OR REPLACE VIEW v_pl_monthly AS
WITH m AS (
    SELECT posting_period,
           COALESCE(SUM(signed_amount) FILTER (WHERE account_type = 'Revenue'), 0)                         AS gross_revenue,
           COALESCE(SUM(signed_amount) FILTER (WHERE account_code = '5000'), 0)                            AS interest_expense,
           COALESCE(SUM(signed_amount) FILTER (WHERE sub_category = 'COGS' AND account_code <> '5000'), 0) AS other_cogs,
           COALESCE(SUM(signed_amount) FILTER (WHERE sub_category = 'Operating Expense'), 0)               AS operating_expense,
           COALESCE(SUM(signed_amount) FILTER (WHERE sub_category = 'Provision'), 0)                       AS loan_loss_provision
    FROM mv_gl_monthly
    WHERE financial_statement = 'PL'
    GROUP BY posting_period
), calc AS (
    SELECT m.*,
           gross_revenue - interest_expense                                           AS net_revenue,
           gross_revenue - interest_expense - other_cogs - operating_expense          AS operating_income
    FROM m
)
SELECT  posting_period,
        gross_revenue, interest_expense, net_revenue, other_cogs, operating_expense,
        operating_income, loan_loss_provision,
        operating_income - loan_loss_provision                                        AS pre_tax_income,
        100.0 * (operating_income - loan_loss_provision) / NULLIF(gross_revenue, 0)   AS pre_tax_margin_pct,
        100.0 * (gross_revenue - LAG(gross_revenue) OVER w) / NULLIF(LAG(gross_revenue) OVER w, 0) AS revenue_mom_pct,
        SUM(gross_revenue)                       OVER (PARTITION BY EXTRACT(YEAR FROM posting_period) ORDER BY posting_period) AS gross_revenue_ytd,
        SUM(operating_income - loan_loss_provision) OVER (PARTITION BY EXTRACT(YEAR FROM posting_period) ORDER BY posting_period) AS pre_tax_income_ytd,
        AVG(gross_revenue) OVER (ORDER BY posting_period ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS gross_revenue_12m_avg,
        COUNT(*)           OVER (ORDER BY posting_period ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) AS _months_in_12m_window
FROM calc
WINDOW w AS (ORDER BY posting_period);
-- NB: gross_revenue_12m_avg is a partial average for the first 11 months; _months_in_12m_window tells you how many.

-- ---------- 3. BALANCE SHEET SNAPSHOT ENGINE ---------------------------------------
-- Account-level cumulative balances at each month-end, with a calendar spine so that months with
-- no activity in an account still carry the prior balance forward (a plain GROUP BY would drop them).
CREATE OR REPLACE VIEW v_balance_sheet_snapshot AS
WITH spine AS (
    SELECT generate_series(MIN(posting_period), MAX(posting_period), INTERVAL '1 month')::date AS posting_period
    FROM mv_gl_monthly
), bs_accts AS (
    SELECT account_id, account_code, account_name, account_type, sub_category, normal_balance
    FROM dim_chart_of_accounts WHERE financial_statement = 'BS'
), mov AS (
    SELECT posting_period, account_id, SUM(signed_amount) AS signed_amount, SUM(net_dr_cr) AS net_dr_cr
    FROM mv_gl_monthly WHERE financial_statement = 'BS' GROUP BY posting_period, account_id
), grid AS (
    SELECT s.posting_period, a.*, COALESCE(m.signed_amount, 0) AS signed_amount, COALESCE(m.net_dr_cr, 0) AS net_dr_cr
    FROM spine s CROSS JOIN bs_accts a
    LEFT JOIN mov m ON m.posting_period = s.posting_period AND m.account_id = a.account_id
)
SELECT  posting_period, account_id, account_code, account_name, account_type, sub_category, normal_balance,
        signed_amount                                                                                  AS period_movement,
        SUM(signed_amount) OVER (PARTITION BY account_id ORDER BY posting_period)                      AS closing_balance_natural,
        SUM(net_dr_cr)     OVER (PARTITION BY account_id ORDER BY posting_period)                      AS closing_net_dr_cr
FROM grid;

-- Accounting-equation check. Because no year-end closing entries are posted in this ledger,
-- equity is Booked Equity + cumulative P&L ("current earnings"). Difference must be 0.00.
CREATE OR REPLACE VIEW v_balance_sheet_check AS
WITH bs AS (
    SELECT posting_period,
           SUM(closing_net_dr_cr)  FILTER (WHERE account_type = 'Asset')     AS assets,             -- contra assets negative
           -SUM(closing_net_dr_cr) FILTER (WHERE account_type = 'Liability') AS liabilities,
           -SUM(closing_net_dr_cr) FILTER (WHERE account_type = 'Equity')    AS booked_equity
    FROM v_balance_sheet_snapshot GROUP BY posting_period
), earn AS (
    SELECT posting_period,
           SUM(SUM(CASE account_type WHEN 'Revenue' THEN signed_amount ELSE -signed_amount END))
               OVER (ORDER BY posting_period) AS cumulative_earnings
    FROM mv_gl_monthly WHERE financial_statement = 'PL' GROUP BY posting_period
)
SELECT bs.posting_period, bs.assets, bs.liabilities, bs.booked_equity,
       COALESCE(e.cumulative_earnings, 0) AS current_earnings,
       bs.liabilities + bs.booked_equity + COALESCE(e.cumulative_earnings, 0) AS liabilities_plus_equity,
       bs.assets - (bs.liabilities + bs.booked_equity + COALESCE(e.cumulative_earnings, 0)) AS difference
FROM bs LEFT JOIN earn e USING (posting_period);
-- Note: if a month has P&L activity but 'earn' is missing for a month with none, cumulative_earnings would
-- be skipped by the GROUP BY; the LEFT JOIN + COALESCE then understates equity. Our ledger has P&L every
-- month; a production version should join earn against the same spine (see docs/limitations.md #7).

-- ---------- 4. VARIANCE ANALYSIS ----------------------------------------------------
-- FULL OUTER JOIN so that (a) budgeted lines with no actuals and (b) unbudgeted actuals both surface.
-- Favourability: Revenue favourable when Actual >= Budget; Expense favourable when Actual <= Budget.
CREATE OR REPLACE VIEW v_variance_analysis AS
WITH act AS (
    SELECT posting_period, account_id, department_id, SUM(signed_amount) AS actual_amount
    FROM mv_gl_monthly WHERE financial_statement = 'PL' GROUP BY 1, 2, 3
), bud AS (
    SELECT make_date(fiscal_year, fiscal_period, 1) AS posting_period, account_id, department_id,
           SUM(budgeted_amount) AS budget_amount
    FROM fact_budget GROUP BY 1, 2, 3
), j AS (
    SELECT COALESCE(a.posting_period, b.posting_period) AS posting_period,
           COALESCE(a.account_id,     b.account_id)     AS account_id,
           COALESCE(a.department_id,  b.department_id)  AS department_id,
           COALESCE(a.actual_amount, 0) AS actual_amount,
           COALESCE(b.budget_amount, 0) AS budget_amount,
           (b.budget_amount IS NULL)    AS is_unbudgeted,
           (a.actual_amount IS NULL)    AS is_no_actuals
    FROM act a FULL OUTER JOIN bud b
      ON a.posting_period = b.posting_period AND a.account_id = b.account_id AND a.department_id = b.department_id
), v AS (
    SELECT j.*, coa.account_code, coa.account_name, coa.account_type, coa.sub_category, d.department_name,
           j.actual_amount - j.budget_amount                         AS variance_amount,          -- Actual - Budget
           100.0 * (j.actual_amount - j.budget_amount) / NULLIF(j.budget_amount, 0) AS variance_pct,
           CASE WHEN coa.account_type = 'Revenue' THEN j.actual_amount - j.budget_amount
                ELSE j.budget_amount - j.actual_amount END           AS favourable_variance        -- + = good
    FROM j JOIN dim_chart_of_accounts coa USING (account_id)
           JOIN dim_department d USING (department_id)
)
SELECT v.*,
       CASE WHEN favourable_variance < 0 THEN 'UNFAVOURABLE' WHEN favourable_variance > 0 THEN 'FAVOURABLE' ELSE 'ON PLAN' END AS variance_flag,
       (favourable_variance < 0)                                                                                  AS is_unfavourable,
       SUM(actual_amount)  OVER ytd AS actual_ytd,
       SUM(budget_amount)  OVER ytd AS budget_ytd,
       100.0 * (SUM(actual_amount) OVER ytd - SUM(budget_amount) OVER ytd) / NULLIF(SUM(budget_amount) OVER ytd, 0) AS variance_pct_ytd,
       variance_amount - LAG(variance_amount) OVER (PARTITION BY account_id, department_id ORDER BY posting_period) AS variance_change_mom
FROM v
WINDOW ytd AS (PARTITION BY account_id, department_id, EXTRACT(YEAR FROM posting_period) ORDER BY posting_period);

-- ---------- 5. LOAN -> GL BRIDGE ----------------------------------------------------
-- Python writes stg_scored_loans; this rolls it up per risk band into fact_provisioning_ledger.
-- EL = SUM(PD x LGD x EAD). Re-runnable per as_of_date (upsert).
INSERT INTO fact_provisioning_ledger (as_of_date, risk_band, expected_loss_amount, account_id, loan_count, ead_total, avg_pd, avg_lgd)
SELECT s.as_of_date, s.risk_band,
       ROUND(SUM(s.pd * s.lgd * s.ead), 2),
       (SELECT account_id FROM dim_chart_of_accounts WHERE account_code = '1210'),
       COUNT(*), SUM(s.ead),
       ROUND(AVG(s.pd), 8)::numeric(9,8), ROUND(AVG(s.lgd), 4)::numeric(5,4)
FROM stg_scored_loans s
GROUP BY s.as_of_date, s.risk_band
ON CONFLICT (as_of_date, risk_band, account_id) DO UPDATE
   SET expected_loss_amount = EXCLUDED.expected_loss_amount, loan_count = EXCLUDED.loan_count,
       ead_total = EXCLUDED.ead_total, avg_pd = EXCLUDED.avg_pd, avg_lgd = EXCLUDED.avg_lgd;

-- Reconciliation, two lenses:
--  (a) BALANCE: modelled EL vs the booked allowance (acct 1210) at that month-end. This is the lens that matters.
--  (b) FLOW: change in modelled EL vs the booked provision charge. Needs >= 2 snapshots; ignores write-offs
--      (allowance utilisation), so treat as an approximation. See docs/limitations.md.
CREATE OR REPLACE VIEW v_provision_recon AS
WITH el AS (
    SELECT as_of_date, SUM(expected_loss_amount) AS el_balance, SUM(ead_total) AS ead_total
    FROM fact_provisioning_ledger GROUP BY as_of_date
), booked AS (
    SELECT posting_period, SUM(signed_amount) AS booked_provision
    FROM mv_gl_monthly WHERE sub_category = 'Provision' GROUP BY posting_period
), allow AS (
    SELECT posting_period, closing_balance_natural AS allowance_balance
    FROM v_balance_sheet_snapshot WHERE account_code = '1210'
)
SELECT el.as_of_date,
       el.ead_total,
       el.el_balance                                                  AS modelled_el,
       al.allowance_balance                                           AS booked_allowance,
       el.el_balance - al.allowance_balance                           AS el_minus_allowance,
       100.0 * el.el_balance   / NULLIF(el.ead_total, 0)              AS modelled_coverage_pct,
       100.0 * al.allowance_balance / NULLIF(el.ead_total, 0)         AS booked_coverage_pct,
       el.el_balance - LAG(el.el_balance) OVER (ORDER BY el.as_of_date) AS model_provision_charge,
       b.booked_provision,
       (el.el_balance - LAG(el.el_balance) OVER (ORDER BY el.as_of_date)) - b.booked_provision AS model_minus_booked_charge
FROM el
LEFT JOIN booked b  ON b.posting_period  = date_trunc('month', el.as_of_date)::date
LEFT JOIN allow  al ON al.posting_period = date_trunc('month', el.as_of_date)::date;

-- ---------- 6. Helper dimension for the Power Pivot "provision basis" toggle ---------
-- (A real table avoids DATATABLE, which Excel's Power Pivot engine does not reliably support.)
CREATE OR REPLACE VIEW dim_provision_basis AS
SELECT * FROM (VALUES (1, 'Booked (GL)'), (2, 'Modelled (EL)')) AS v(basis_id, basis_name);

-- ---------- 8. Net Interest Margin (annualised, on average gross loans) ----------
-- Earning assets = gross loans (acct 1200); cash earns nothing in this COA. First month has no opening balance -> NULL.
CREATE OR REPLACE VIEW v_nim_monthly AS
WITH nii AS (
    SELECT posting_period,
           COALESCE(SUM(signed_amount) FILTER (WHERE account_code = '4000'), 0)
         - COALESCE(SUM(signed_amount) FILTER (WHERE account_code = '5000'), 0) AS net_interest_income
    FROM mv_gl_monthly GROUP BY posting_period
), loans AS (
    SELECT posting_period, closing_balance_natural AS closing_loans,
           LAG(closing_balance_natural) OVER (ORDER BY posting_period) AS opening_loans
    FROM v_balance_sheet_snapshot WHERE account_code = '1200'
)
SELECT n.posting_period, n.net_interest_income, l.opening_loans, l.closing_loans,
       (l.opening_loans + l.closing_loans) / 2                                            AS avg_earning_assets,
       100.0 * 12 * n.net_interest_income / NULLIF((l.opening_loans + l.closing_loans) / 2, 0) AS nim_annualised_pct
FROM nii n JOIN loans l USING (posting_period);
