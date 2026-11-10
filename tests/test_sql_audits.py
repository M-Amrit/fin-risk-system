"""
SQL data-quality tests. Require a loaded database (see README). Configure with the standard libpq env vars
(PGHOST, PGDATABASE, PGUSER, PGPASSWORD). Skipped automatically if psql/DB are unavailable.
Negative tests run inside a transaction that is ALWAYS rolled back.
"""
import shutil, subprocess
import pytest

def psql(sql):
    r = subprocess.run(["psql", "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()

def psql_script(sql):
    r = subprocess.run(["psql", "-At", "-F", "|", "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout

@pytest.fixture(scope="module", autouse=True)
def _db():
    if not shutil.which("psql"):
        pytest.skip("psql not installed")
    try:
        if psql("SELECT count(*) FROM fin.fact_general_ledger") == "0":
            pytest.skip("database not loaded")
    except RuntimeError:
        pytest.skip("database unavailable")

def failing_checks():
    rows = psql("SELECT check_name FROM fin.v_dq_audit WHERE status='FAIL' ORDER BY 1")
    return [r for r in rows.splitlines() if r]

def test_clean_load_passes_every_audit():
    assert failing_checks() == []

def test_audits_detect_unbalanced_journal_and_broken_period():
    out = psql_script("""
        SET search_path = fin, public;
        BEGIN;
        INSERT INTO fact_general_ledger VALUES (9000001, 9000001, '2023-01-15', 1, 1, 100.00, 0, 'bad dr', '2023-01-01');
        INSERT INTO fact_general_ledger VALUES (9000002, 9000001, '2023-03-15', 2, 1, 0, 90.00, 'bad cr', '2023-01-01');
        SELECT 'FAILS:' || string_agg(check_name, ',' ORDER BY check_name) FROM v_dq_audit WHERE status='FAIL';
        ROLLBACK;
    """)
    line = [l for l in out.splitlines() if l.startswith("FAILS:")][0]
    for expected in ("unbalanced_journal_batches", "ledger_total_dr_ne_cr", "txn_date_outside_posting_period"):
        assert expected in line, line
    assert failing_checks() == []                                      # rollback left no residue

def test_audits_detect_orphans_when_constraints_bypassed():
    out = psql_script("""
        SET search_path = fin, public;
        BEGIN;
        SET LOCAL session_replication_role = replica;                  -- disables FK triggers (superuser) to simulate a bad bulk load
        INSERT INTO fact_loan_portfolio VALUES (99999999, 424242424, '2018-01-01', 1000, 10, 'A', 'A1', 'Current', 36);
        SELECT 'FAILS:' || string_agg(check_name, ',') FROM v_dq_audit WHERE status='FAIL';
        ROLLBACK;
    """)
    line = [l for l in out.splitlines() if l.startswith("FAILS:")][0]
    assert "orphan_loan_borrower" in line, line

def test_balance_sheet_balances_every_month():
    assert psql("SELECT count(*) FROM fin.v_balance_sheet_check WHERE abs(difference) > 0.01") == "0"
