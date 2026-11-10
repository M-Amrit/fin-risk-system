-- =====================================================================================
-- 04_board_kpis.sql | One-row KPI feed read by the Power Automate board-pack flow.
-- Depends on 02 (views) and 03 (v_dq_audit). Run order: 01, 00, 00b, 02, 03, 04. Re-run 03 and 04 after any re-run of 02
-- (02 drops mv_gl_monthly CASCADE, which drops dependent views). For routine refreshes use REFRESH MATERIALIZED VIEW instead.
-- =====================================================================================
SET search_path = fin, public;

-- Variance % is computed at ACCOUNT level (sum across departments first) so it ties to the DAX [Variance %] measure.
CREATE OR REPLACE VIEW v_board_kpis AS
WITH last AS (SELECT MAX(posting_period) AS p FROM mv_gl_monthly),
pl  AS (SELECT * FROM v_pl_monthly WHERE posting_period = (SELECT p FROM last)),
acct AS (
    SELECT account_id, SUM(favourable_variance) AS fav, ABS(SUM(budget_amount)) AS base
    FROM v_variance_analysis WHERE posting_period = (SELECT p FROM last) GROUP BY account_id
),
var AS (SELECT SUM(fav) AS fav, SUM(base) AS base FROM acct),
el  AS (SELECT as_of_date, SUM(expected_loss_amount) AS expected_loss, SUM(ead_total) AS ead
        FROM fact_provisioning_ledger GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 1),
dq  AS (SELECT COUNT(*) FILTER (WHERE status = 'FAIL') AS dq_failures FROM v_dq_audit)
SELECT (SELECT p FROM last)                              AS reporting_period,
       pl.gross_revenue, pl.net_revenue, pl.operating_income,
       pl.pre_tax_income                                 AS net_income,          -- no tax accounts: pre-tax = net
       el.as_of_date                                     AS el_as_of_date,
       el.expected_loss, el.ead,
       100.0 * el.expected_loss / NULLIF(el.ead, 0)      AS el_pct_of_ead,
       100.0 * var.fav / NULLIF(var.base, 0)             AS budget_variance_pct,  -- + = favourable
       dq.dq_failures,
       ((DATE_PART('year', (SELECT p FROM last)) - DATE_PART('year', el.as_of_date)) * 12
         + DATE_PART('month', (SELECT p FROM last)) - DATE_PART('month', el.as_of_date))::int AS el_months_stale  -- gate: > 1 = stale model
FROM pl CROSS JOIN var CROSS JOIN dq LEFT JOIN el ON TRUE;
