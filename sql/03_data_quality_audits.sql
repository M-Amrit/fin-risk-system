-- =====================================================================================
-- 03_data_quality_audits.sql | One row per check: (check_name, violations, status)
-- Gate the board-pack pipeline on: SELECT COUNT(*) FROM fin.v_dq_audit WHERE status = 'FAIL';  -> must be 0
-- Tolerance for monetary reconciliation is 0.01 (one cent).
-- =====================================================================================
SET search_path = fin, public;

CREATE OR REPLACE VIEW v_dq_audit AS
WITH checks AS (
    -- Referential integrity (FKs enforce this on insert; audits catch loads done with constraints disabled)
    SELECT 'orphan_gl_account' AS check_name, COUNT(*) AS violations
      FROM fact_general_ledger g LEFT JOIN dim_chart_of_accounts c USING (account_id) WHERE c.account_id IS NULL
    UNION ALL SELECT 'orphan_gl_department', COUNT(*)
      FROM fact_general_ledger g LEFT JOIN dim_department d USING (department_id) WHERE d.department_id IS NULL
    UNION ALL SELECT 'orphan_budget_account', COUNT(*)
      FROM fact_budget b LEFT JOIN dim_chart_of_accounts c USING (account_id) WHERE c.account_id IS NULL
    UNION ALL SELECT 'orphan_loan_borrower', COUNT(*)
      FROM fact_loan_portfolio l LEFT JOIN dim_borrower b USING (borrower_id) WHERE b.borrower_id IS NULL
    UNION ALL SELECT 'orphan_performance_loan', COUNT(*)
      FROM fact_credit_performance p LEFT JOIN fact_loan_portfolio l USING (loan_id) WHERE l.loan_id IS NULL
    UNION ALL SELECT 'orphan_scored_loan', COUNT(*)
      FROM stg_scored_loans s LEFT JOIN fact_loan_portfolio l USING (loan_id) WHERE l.loan_id IS NULL
    -- Double-entry integrity
    UNION ALL SELECT 'unbalanced_journal_batches', COUNT(*) FROM (
        SELECT entry_batch_id FROM fact_general_ledger
        GROUP BY entry_batch_id HAVING ABS(SUM(debit_amount) - SUM(credit_amount)) > 0.01) x
    UNION ALL SELECT 'single_line_batches', COUNT(*) FROM (
        SELECT entry_batch_id FROM fact_general_ledger GROUP BY entry_batch_id HAVING COUNT(*) < 2) x
    UNION ALL SELECT 'ledger_total_dr_ne_cr', CASE WHEN ABS(SUM(debit_amount) - SUM(credit_amount)) > 0.01 THEN 1 ELSE 0 END
      FROM fact_general_ledger
    -- Period integrity
    UNION ALL SELECT 'txn_date_outside_posting_period', COUNT(*)
      FROM fact_general_ledger WHERE date_trunc('month', transaction_date)::date <> posting_period
    UNION ALL SELECT 'zero_value_lines', COUNT(*)
      FROM fact_general_ledger WHERE debit_amount = 0 AND credit_amount = 0
    -- Accounting equation at every month-end
    UNION ALL SELECT 'balance_sheet_not_balancing_months', COUNT(*)
      FROM v_balance_sheet_check WHERE ABS(difference) > 0.01
    -- Budget
    UNION ALL SELECT 'unbudgeted_actuals', COUNT(*)
      FROM v_variance_analysis WHERE is_unbudgeted AND actual_amount <> 0
    UNION ALL SELECT 'negative_natural_budget', COUNT(*)
      FROM fact_budget WHERE budgeted_amount < 0
    -- Credit
    UNION ALL SELECT 'loan_status_default_without_dpd90_or_chargeoff', COUNT(*)
      FROM fact_loan_portfolio l WHERE l.loan_status = 'Default' AND NOT EXISTS (
          SELECT 1 FROM fact_credit_performance p WHERE p.loan_id = l.loan_id AND p.days_past_due >= 90)
    UNION ALL SELECT 'scored_pd_out_of_range', COUNT(*) FROM stg_scored_loans WHERE pd <= 0 OR pd >= 1
    UNION ALL SELECT 'duplicate_loan_snapshots', COUNT(*) FROM (
        SELECT loan_id, as_of_date FROM fact_credit_performance GROUP BY 1, 2 HAVING COUNT(*) > 1) x
    -- Bridge reconciliation: provisioning ledger EAD must equal scored EAD for the same date
    UNION ALL SELECT 'provision_ledger_ead_mismatch', COUNT(*) FROM (
        SELECT s.as_of_date FROM (SELECT as_of_date, SUM(ead) e FROM stg_scored_loans GROUP BY 1) s
        LEFT JOIN (SELECT as_of_date, SUM(ead_total) e FROM fact_provisioning_ledger GROUP BY 1) p USING (as_of_date)
        WHERE ABS(s.e - COALESCE(p.e, 0)) > 0.01) x
)
SELECT check_name, violations, CASE WHEN violations = 0 THEN 'PASS' ELSE 'FAIL' END AS status FROM checks;
