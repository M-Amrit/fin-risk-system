-- Run from repo root after 01_ddl.sql and 00_load_synthetic.sql:  psql -d finrisk -f sql/00b_load_loans.sql
SET search_path = fin, public;
\copy dim_borrower (borrower_id,age,housing_status,employment_length,annual_income,dti_ratio) FROM 'data/synthetic/dim_borrower.csv' CSV HEADER
\copy fact_loan_portfolio (loan_id,borrower_id,issue_date,loan_amount,interest_rate,grade,sub_grade,loan_status,repayment_term) FROM 'data/synthetic/fact_loan_portfolio.csv' CSV HEADER
\copy fact_credit_performance (performance_id,loan_id,as_of_date,days_past_due,revolving_utilization,delinquency_2yrs,revolving_balance,outstanding_principal,recovery_amount) FROM 'data/synthetic/fact_credit_performance.csv' CSV HEADER
\copy stg_scored_loans (loan_id,as_of_date,score,pd,risk_band,ead,lgd) FROM 'data/scorecard_out/stg_scored_loans.csv' CSV HEADER
