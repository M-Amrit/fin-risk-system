-- Run from repo root:  psql -d finrisk -f sql/00_load_synthetic.sql   (after 01_ddl.sql)
-- Uses client-side \copy so the CSVs need not be readable by the server process.
SET search_path = fin, public;
\copy dim_chart_of_accounts (account_id,account_code,account_name,account_type,financial_statement,sub_category,normal_balance) FROM 'data/synthetic/dim_chart_of_accounts.csv' CSV HEADER
\copy dim_department (department_id,department_code,department_name,cost_center) FROM 'data/synthetic/dim_department.csv' CSV HEADER
\copy fact_general_ledger (journal_id,entry_batch_id,transaction_date,account_id,department_id,debit_amount,credit_amount,description,posting_period) FROM 'data/synthetic/fact_general_ledger.csv' CSV HEADER
\copy fact_budget (budget_id,fiscal_year,fiscal_period,account_id,department_id,budgeted_amount) FROM 'data/synthetic/fact_budget.csv' CSV HEADER
