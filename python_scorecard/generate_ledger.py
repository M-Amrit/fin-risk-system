"""
Deterministic synthetic general ledger + budget generator (double-entry).

Design rules
------------
* Every journal batch (entry_batch_id) balances to the cent: SUM(debit) == SUM(credit).
* Cash is the balancing account for operating flows; opening capital + deposits fund the loan book.
* Budget = driver-based plan, in NATURAL sign (revenue and expense both stored positive).
* The link to the credit module is BY CONSTRUCTION (a parameterised loan-book schedule), not by
  observation. LendingClub borrowers never sat on this bank's balance sheet. See docs/limitations.md.
* Faker is intentionally not used: nothing here needs fake names, and seeded numpy is fully
  reproducible across library versions.

Usage: python generate_ledger.py --out ../data/synthetic --months 36 --start 2023-01 --seed 42
"""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd

# account_code, name, type, statement, sub_category, normal_balance
COA = [
    ("1000", "Cash and Equivalents",        "Asset",     "BS", "Current Assets",      "D"),
    ("1200", "Loans Receivable (Gross)",    "Asset",     "BS", "Earning Assets",      "D"),
    ("1210", "Allowance for Credit Losses", "Asset",     "BS", "Contra Asset",        "C"),
    ("1500", "Fixed Assets (Net)",          "Asset",     "BS", "Non-Current Assets",  "D"),
    ("2000", "Customer Deposits",           "Liability", "BS", "Funding",             "C"),
    ("2100", "Accrued Liabilities",         "Liability", "BS", "Current Liabilities", "C"),
    ("3000", "Share Capital",               "Equity",    "BS", "Paid-in Capital",     "C"),
    ("3100", "Retained Earnings",           "Equity",    "BS", "Retained Earnings",   "C"),
    ("4000", "Interest Income - Loans",     "Revenue",   "PL", "Interest Income",     "C"),
    ("4100", "Fee Income",                  "Revenue",   "PL", "Non-Interest Income", "C"),
    ("5000", "Interest Expense - Deposits", "Expense",   "PL", "COGS",                "D"),
    ("5100", "Loan Servicing Costs",        "Expense",   "PL", "COGS",                "D"),
    ("6000", "Salaries and Benefits",       "Expense",   "PL", "Operating Expense",   "D"),
    ("6100", "Technology and Cloud",        "Expense",   "PL", "Operating Expense",   "D"),
    ("6200", "Marketing",                   "Expense",   "PL", "Operating Expense",   "D"),
    ("6300", "Rent and Facilities",         "Expense",   "PL", "Operating Expense",   "D"),
    ("7000", "Provision for Credit Losses", "Expense",   "PL", "Provision",           "D"),
]
DEPTS = [("LND", "Lending Operations", "CC100"), ("RSK", "Credit Risk", "CC200"),
         ("TEC", "Technology", "CC300"), ("MKT", "Marketing", "CC400"), ("COR", "Corporate", "CC900")]


def build(start, months, seed, opening_capital, start_book, book_growth, loan_yield, funding_cost, el_rate, split=1, size=1.0):
    rng = np.random.default_rng(seed)
    coa = pd.DataFrame(COA, columns=["account_code", "account_name", "account_type", "financial_statement",
                                     "sub_category", "normal_balance"])
    coa.insert(0, "account_id", range(1, len(coa) + 1))
    dept = pd.DataFrame(DEPTS, columns=["department_code", "department_name", "cost_center"])
    dept.insert(0, "department_id", range(1, len(dept) + 1))
    aid = dict(zip(coa.account_code, coa.account_id))
    did = dict(zip(dept.department_code, dept.department_id))

    periods = pd.date_range(start=f"{start}-01", periods=months, freq="MS")
    rows, budget = [], []
    batch = 0

    def post(period, day, lines, desc):
        """lines: (account_code, dept_code, debit, credit). Rounded BEFORE the balance check.
        With split>1, each two-line journal is exploded into `split` smaller balanced journals
        (volume stress-testing); the monthly totals are unchanged."""
        nonlocal batch
        lines = [(c, dc, round(dr, 2), round(cr, 2)) for c, dc, dr, cr in lines]
        assert abs(sum(l[2] for l in lines) - sum(l[3] for l in lines)) < 0.005, desc
        k = split if (split > 1 and len(lines) == 2 and desc != "Opening capital") else 1
        if k > 1:
            w = rng.dirichlet(np.ones(k))
            cum = np.round(np.cumsum(w) * lines[0][2] + np.cumsum(w) * lines[0][3], 2)
            parts = np.diff(np.concatenate([[0.0], cum]))
            parts[-1] = round(lines[0][2] + lines[0][3] - parts[:-1].sum(), 2)
            amounts = [round(float(x), 2) for x in parts]
        else:
            amounts = [None]
        for j, amt in enumerate(amounts):
            batch += 1
            dday = min(int(day) + (j % 3), 28) if k > 1 else int(day)
            d = period + pd.Timedelta(days=min(dday, period.days_in_month - 1))
            for code, dc, dr, cr in lines:
                if amt is not None:
                    dr, cr = (amt, 0.0) if dr > 0 else (0.0, amt)
                rows.append((batch, d.date(), aid[code], did[dc], dr, cr, desc, period.date()))

    post(periods[0], 0, [("1000", "COR", opening_capital, 0), ("3000", "COR", 0, opening_capital)],
         "Opening capital")

    book = allowance = deposits = 0.0
    for i, p in enumerate(periods):
        target_book = start_book * (1 + book_growth) ** i * rng.normal(1, 0.01)
        disb = target_book - book
        new_dep = max(0.0, 0.85 * disb)
        if abs(disb) > 0.005:
            if disb > 0:
                post(p, 2, [("1200", "LND", disb, 0), ("1000", "LND", 0, disb)], "Loan disbursements")
            else:
                post(p, 2, [("1000", "LND", -disb, 0), ("1200", "LND", 0, -disb)], "Net loan repayments")
        if new_dep > 0:
            post(p, 3, [("1000", "COR", new_dep, 0), ("2000", "COR", 0, new_dep)], "Deposit inflow")
        avg_book = (book + target_book) / 2
        book, deposits = target_book, deposits + new_dep

        interest = avg_book * loan_yield / 12 * rng.normal(1, 0.015)
        fees = interest * 0.07 * rng.normal(1, 0.05)
        int_exp = deposits * funding_cost / 12 * rng.normal(1, 0.02)
        servicing = avg_book * 0.0035 / 12 * rng.normal(1, 0.04)
        sal = size * 410_000 * (1.004 ** i) * rng.normal(1, 0.01)
        tech = size * 120_000 * (1.006 ** i) * rng.normal(1, 0.05)
        mkt = size * 85_000 * rng.normal(1, 0.15)
        rent = size * 45_000
        post(p, 27, [("1000", "LND", interest, 0), ("4000", "LND", 0, interest)], "Interest income received")
        post(p, 27, [("1000", "LND", fees, 0), ("4100", "LND", 0, fees)], "Fee income")
        post(p, 28, [("5000", "COR", int_exp, 0), ("1000", "COR", 0, int_exp)], "Deposit interest paid")
        post(p, 28, [("5100", "LND", servicing, 0), ("1000", "LND", 0, servicing)], "Servicing costs")
        post(p, 28, [("6000", "COR", sal, 0), ("1000", "COR", 0, sal)], "Payroll")
        post(p, 28, [("6100", "TEC", tech, 0), ("1000", "TEC", 0, tech)], "Cloud/tech spend")
        post(p, 28, [("6200", "MKT", mkt, 0), ("1000", "MKT", 0, mkt)], "Marketing spend")
        post(p, 28, [("6300", "COR", rent, 0), ("1000", "COR", 0, rent)], "Rent")

        target_allow = book * el_rate * rng.normal(1, 0.03)
        prov = target_allow - allowance
        if prov > 0:
            post(p, 29, [("7000", "RSK", prov, 0), ("1210", "RSK", 0, prov)], "Provision charge")
        else:
            post(p, 29, [("1210", "RSK", -prov, 0), ("7000", "RSK", 0, -prov)], "Provision release")
        allowance = target_allow

        plan_book = start_book * (1 + book_growth) ** i
        prev_plan = start_book * (1 + book_growth) ** (i - 1) if i > 0 else 0.0
        plan = {"4000": ("LND", plan_book * loan_yield / 12),
                "4100": ("LND", plan_book * loan_yield / 12 * 0.07),
                "5000": ("COR", plan_book * 0.85 * funding_cost / 12),
                "5100": ("LND", plan_book * 0.0035 / 12),
                "6000": ("COR", size * 410_000 * (1.004 ** i)),
                "6100": ("TEC", size * 115_000 * (1.006 ** i)),
                "6200": ("MKT", size * 90_000.0),
                "6300": ("COR", size * 45_000.0),
                "7000": ("RSK", max((plan_book - prev_plan) * el_rate, 0.0))}
        for code, (dc, amt) in plan.items():
            budget.append((int(p.year), int(p.month), aid[code], did[dc], round(float(amt), 2)))

    gl = pd.DataFrame(rows, columns=["entry_batch_id", "transaction_date", "account_id", "department_id",
                                     "debit_amount", "credit_amount", "description", "posting_period"])
    gl.insert(0, "journal_id", range(1, len(gl) + 1))
    bud = pd.DataFrame(budget, columns=["fiscal_year", "fiscal_period", "account_id", "department_id",
                                        "budgeted_amount"])
    bud.insert(0, "budget_id", range(1, len(bud) + 1))
    return coa, dept, gl, bud


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../data/synthetic")
    ap.add_argument("--start", default="2023-01")
    ap.add_argument("--months", type=int, default=36)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--opening-capital", type=float, default=None, help="default 0.61 x start-book")
    ap.add_argument("--start-book", type=float, default=180_000_000)
    ap.add_argument("--book-growth", type=float, default=0.02)
    ap.add_argument("--loan-yield", type=float, default=0.13)
    ap.add_argument("--funding-cost", type=float, default=0.045)
    ap.add_argument("--el-rate", type=float, default=0.045)
    ap.add_argument("--anchor-ead", type=float, default=None, help="force the gross loan book at --anchor-date to this EAD (ties GL to scorecard)")
    ap.add_argument("--anchor-date", default="2019-03-31")
    ap.add_argument("--split", type=int, default=1, help="explode each journal into N journals (volume test)")
    a = ap.parse_args()
    if a.anchor_ead:
        ad, st = pd.Timestamp(a.anchor_date), pd.Timestamp(f"{a.start}-01")
        k = (ad.year - st.year) * 12 + ad.month - st.month
        assert 0 <= k < a.months, "anchor-date outside ledger range"
        a.start_book = a.anchor_ead / (1 + a.book_growth) ** k
    if a.opening_capital is None:
        a.opening_capital = 0.61 * a.start_book
    size = a.start_book / 180_000_000
    coa, dept, gl, bud = build(a.start, a.months, a.seed, a.opening_capital, a.start_book, a.book_growth,
                               a.loan_yield, a.funding_cost, a.el_rate, a.split, size)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    coa.to_csv(out / "dim_chart_of_accounts.csv", index=False)
    dept.to_csv(out / "dim_department.csv", index=False)
    gl.to_csv(out / "fact_general_ledger.csv", index=False)
    bud.to_csv(out / "fact_budget.csv", index=False)
    print(f"GL rows={len(gl):,} batches={gl.entry_batch_id.nunique():,} budget rows={len(bud):,}")
    print(f"Debits={gl.debit_amount.sum():,.2f} Credits={gl.credit_amount.sum():,.2f}")
