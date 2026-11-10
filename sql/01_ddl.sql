-- =====================================================================================
-- 01_ddl.sql  |  Financial Analytics & Risk Reporting System  |  PostgreSQL 14+ (tested on 16)
-- Star schema in schema `fin`. Idempotent: safe to re-run on an empty database.
--
-- Deviations from the written spec (each is deliberate):
--   [D1] dim_chart_of_accounts.normal_balance   - signing debits/credits needs it; deriving it
--                                                 from account_type breaks for contra accounts (1210).
--   [D2] fact_general_ledger.entry_batch_id     - spec's journal_id is a LINE id; without a batch key
--                                                 you cannot test that a journal balances.
--   [D3] fact_credit_performance.outstanding_principal / recovery_amount - EAD and LGD need them.
--   [D4] dim_date                               - Power Pivot time intelligence requires a date table.
--   [D5] fact_provisioning_ledger extra columns  - audit trail for how EL was produced.
--   [D6] stg_scored_loans                       - landing table for the Python scorecard output.
-- =====================================================================================
CREATE SCHEMA IF NOT EXISTS fin;
SET search_path = fin, public;

-- ---------- Module 1: General ledger & corporate finance ----------------------------
CREATE TABLE IF NOT EXISTS dim_chart_of_accounts (
    account_id           INTEGER PRIMARY KEY,
    account_code         VARCHAR(10)  NOT NULL UNIQUE,
    account_name         VARCHAR(120) NOT NULL,
    account_type         VARCHAR(10)  NOT NULL CHECK (account_type IN ('Asset','Liability','Equity','Revenue','Expense')),
    financial_statement  VARCHAR(2)   NOT NULL CHECK (financial_statement IN ('PL','BS')),
    sub_category         VARCHAR(60)  NOT NULL,
    normal_balance       CHAR(1)      NOT NULL CHECK (normal_balance IN ('D','C')),            -- [D1]
    CONSTRAINT ck_stmt_type CHECK (
        (financial_statement = 'BS' AND account_type IN ('Asset','Liability','Equity')) OR
        (financial_statement = 'PL' AND account_type IN ('Revenue','Expense')))
);

CREATE TABLE IF NOT EXISTS dim_department (
    department_id    INTEGER PRIMARY KEY,
    department_code  VARCHAR(10) NOT NULL UNIQUE,
    department_name  VARCHAR(80) NOT NULL,
    cost_center      VARCHAR(10) NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_general_ledger (
    journal_id        BIGINT PRIMARY KEY,                                                     -- line id (spec)
    entry_batch_id    BIGINT NOT NULL,                                                        -- [D2]
    transaction_date  DATE   NOT NULL,
    account_id        INTEGER NOT NULL REFERENCES dim_chart_of_accounts(account_id),
    department_id     INTEGER NOT NULL REFERENCES dim_department(department_id),
    debit_amount      NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (debit_amount  >= 0),
    credit_amount     NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK (credit_amount >= 0),
    description       VARCHAR(200),
    posting_period    DATE   NOT NULL CHECK (posting_period = date_trunc('month', posting_period)::date),
    CONSTRAINT ck_one_sided CHECK (NOT (debit_amount > 0 AND credit_amount > 0))
);
CREATE INDEX IF NOT EXISTS ix_gl_period_acct  ON fact_general_ledger (posting_period, account_id) INCLUDE (department_id, debit_amount, credit_amount);
CREATE INDEX IF NOT EXISTS ix_gl_batch        ON fact_general_ledger (entry_batch_id);
CREATE INDEX IF NOT EXISTS ix_gl_dept         ON fact_general_ledger (department_id);
CREATE INDEX IF NOT EXISTS ix_gl_period_brin  ON fact_general_ledger USING brin (posting_period);

CREATE TABLE IF NOT EXISTS fact_budget (
    budget_id        BIGINT PRIMARY KEY,
    fiscal_year      SMALLINT NOT NULL,
    fiscal_period    SMALLINT NOT NULL CHECK (fiscal_period BETWEEN 1 AND 12),   -- assumes fiscal year = calendar year
    account_id       INTEGER NOT NULL REFERENCES dim_chart_of_accounts(account_id),
    department_id    INTEGER NOT NULL REFERENCES dim_department(department_id),
    budgeted_amount  NUMERIC(18,2) NOT NULL,                                      -- NATURAL sign (rev and exp both positive)
    CONSTRAINT uq_budget_grain UNIQUE (fiscal_year, fiscal_period, account_id, department_id)
);

CREATE TABLE IF NOT EXISTS dim_date (                                                      -- [D4]
    date_key       DATE PRIMARY KEY,
    year           SMALLINT NOT NULL,
    month          SMALLINT NOT NULL,
    month_start    DATE     NOT NULL,
    fiscal_year    SMALLINT NOT NULL,
    fiscal_period  SMALLINT NOT NULL,
    year_month     CHAR(7)  NOT NULL
);

-- ---------- Module 2: Credit loan portfolio -----------------------------------------
CREATE TABLE IF NOT EXISTS dim_borrower (
    borrower_id        BIGINT PRIMARY KEY,
    age                SMALLINT,               -- LendingClub does NOT publish age; NULL there. Home Credit has DAYS_BIRTH.
    housing_status     VARCHAR(20),
    employment_length  NUMERIC(4,1),           -- years; NULL = unknown (kept distinct from 0)
    annual_income      NUMERIC(14,2),
    dti_ratio          NUMERIC(7,2)
);

CREATE TABLE IF NOT EXISTS fact_loan_portfolio (
    loan_id          BIGINT PRIMARY KEY,
    borrower_id      BIGINT NOT NULL REFERENCES dim_borrower(borrower_id),
    issue_date       DATE NOT NULL,
    loan_amount      NUMERIC(14,2) NOT NULL CHECK (loan_amount > 0),
    interest_rate    NUMERIC(6,3),
    grade            CHAR(1),
    sub_grade        CHAR(2),
    loan_status      VARCHAR(20) NOT NULL CHECK (loan_status IN ('Current','Fully Paid','Charged Off','Default')),
    repayment_term   SMALLINT NOT NULL                                                    -- months
);
CREATE INDEX IF NOT EXISTS ix_loan_borrower ON fact_loan_portfolio (borrower_id);
CREATE INDEX IF NOT EXISTS ix_loan_issue    ON fact_loan_portfolio (issue_date);
CREATE INDEX IF NOT EXISTS ix_loan_status   ON fact_loan_portfolio (loan_status);

CREATE TABLE IF NOT EXISTS fact_credit_performance (
    performance_id         BIGINT PRIMARY KEY,
    loan_id                BIGINT NOT NULL REFERENCES fact_loan_portfolio(loan_id),
    as_of_date             DATE NOT NULL,
    days_past_due          INTEGER NOT NULL DEFAULT 0 CHECK (days_past_due >= 0),
    revolving_utilization  NUMERIC(7,2),
    delinquency_2yrs       SMALLINT,
    revolving_balance      NUMERIC(14,2),
    outstanding_principal  NUMERIC(14,2),                                                  -- [D3]
    recovery_amount        NUMERIC(14,2) DEFAULT 0,                                        -- [D3]
    CONSTRAINT uq_perf_snapshot UNIQUE (loan_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS ix_perf_asof ON fact_credit_performance (as_of_date) INCLUDE (loan_id, days_past_due);

-- ---------- Module 3: Bridge (loans -> GL) ------------------------------------------
CREATE TABLE IF NOT EXISTS stg_scored_loans (                                              -- [D6]
    loan_id        BIGINT NOT NULL,
    as_of_date     DATE   NOT NULL,
    score          INTEGER NOT NULL,
    pd             NUMERIC(9,8) NOT NULL CHECK (pd BETWEEN 0 AND 1),
    risk_band      VARCHAR(20) NOT NULL,
    ead            NUMERIC(14,2) NOT NULL CHECK (ead >= 0),
    lgd            NUMERIC(5,4)  NOT NULL CHECK (lgd BETWEEN 0 AND 1),
    PRIMARY KEY (loan_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS fact_provisioning_ledger (
    entry_id             BIGSERIAL PRIMARY KEY,
    as_of_date           DATE NOT NULL,
    risk_band            VARCHAR(20) NOT NULL,
    expected_loss_amount NUMERIC(18,2) NOT NULL CHECK (expected_loss_amount >= 0),
    account_id           INTEGER NOT NULL REFERENCES dim_chart_of_accounts(account_id),    -- allowance account (1210)
    loan_count           INTEGER,                                                          -- [D5]
    ead_total            NUMERIC(18,2),                                                    -- [D5]
    avg_pd               NUMERIC(9,8),                                                     -- [D5]
    avg_lgd              NUMERIC(5,4),                                                     -- [D5]
    CONSTRAINT uq_prov_grain UNIQUE (as_of_date, risk_band, account_id)
);

-- ---------- Signed-amount semantic layer -------------------------------------------
-- Sign convention: signed_amount > 0 means "increases the account in its natural direction".
--   Revenue +credit, Expense +debit, Asset +debit, Liability/Equity +credit. Contra-asset 1210 is
--   normal_balance 'C', so its signed balance is positive when it has a credit balance.
CREATE OR REPLACE VIEW v_gl_signed AS
SELECT  gl.journal_id, gl.entry_batch_id, gl.transaction_date, gl.posting_period,
        gl.account_id, coa.account_code, coa.account_name, coa.account_type,
        coa.financial_statement, coa.sub_category, coa.normal_balance,
        gl.department_id,
        gl.debit_amount, gl.credit_amount,
        gl.debit_amount - gl.credit_amount AS net_dr_cr,
        CASE coa.normal_balance WHEN 'D' THEN gl.debit_amount - gl.credit_amount
                                ELSE         gl.credit_amount - gl.debit_amount END AS signed_amount
FROM fact_general_ledger gl
JOIN dim_chart_of_accounts coa USING (account_id);
