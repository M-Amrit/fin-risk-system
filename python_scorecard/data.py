"""
Loan data layer.
  * synthetic_loans()      - LendingClub-shaped synthetic book for offline runs / CI.
  * load_lendingclub()     - real LendingClub 'accepted_2007_to_2018' CSV -> same schema.
  * export_star()          - writes CSVs matching fin.dim_borrower / fact_loan_portfolio / fact_credit_performance.

IMPORTANT: results on synthetic_loans() prove the CODE works. They say nothing about real-world model
quality, because the data are generated from a logistic model, which is exactly what we then fit.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.special import expit

NUMERIC = ["loan_amnt", "term", "annual_inc", "dti", "revol_util", "revol_bal", "fico", "emp_length",
           "inq_last_6mths", "delinq_2yrs", "open_acc", "total_acc", "pub_rec"]
CATEGORICAL = ["home_ownership", "purpose", "verification_status"]
LEAKY = ["int_rate", "grade", "sub_grade"]       # LendingClub's own risk output; unusable as an origination feature


def synthetic_loans(n: int = 200_000, seed: int = 7, target_bad_rate: float = 0.16,
                    as_of: str = "2019-03-31", resolved_before: str = "2017-07-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    months = rng.integers(0, 42, n)                                       # Jan-2015 .. Jun-2018
    issue = pd.to_datetime("2015-01-01") + pd.to_timedelta(months * 30.4375 + rng.integers(0, 28, n), unit="D")
    mu_inc = 10.95 + 0.004 * months                                       # income drift -> non-zero PSI later
    annual_inc = np.exp(rng.normal(mu_inc, 0.55))
    annual_inc[rng.random(n) < 0.001] *= 20                               # extreme outliers
    dti = np.clip(rng.normal(18, 8, n), 0, 60)
    dti[rng.random(n) < 0.003] = 999                                      # data-entry style junk seen in real data
    revol_util = np.clip(rng.normal(52, 25, n), 0, 150)
    fico = np.clip(np.round(rng.normal(695, 30, n) / 5) * 5, 625, 850)
    emp = rng.integers(0, 11, n).astype(float)
    term = np.where(rng.random(n) < 0.75, 36, 60)
    loan_amnt = np.clip(np.round(np.exp(rng.normal(9.4, 0.65, n)) / 25) * 25, 1000, 40000)
    home = rng.choice(["RENT", "MORTGAGE", "OWN", "OTHER"], n, p=[.40, .48, .11, .01])
    purpose = rng.choice(["debt_consolidation", "credit_card", "home_improvement", "other", "small_business",
                          "medical", "major_purchase"], n, p=[.58, .20, .06, .10, .02, .02, .02])
    verif = rng.choice(["Verified", "Source Verified", "Not Verified"], n, p=[.30, .35, .35])
    inq = rng.poisson(0.7, n); delinq = rng.poisson(0.3, n); pub = rng.poisson(0.15, n)
    open_acc = rng.poisson(11, n) + 1; total_acc = open_acc + rng.poisson(12, n)
    revol_bal = np.exp(rng.normal(9.3, 1.0, n))

    z0 = (0.030 * (np.minimum(dti, 60) - 18) - 0.013 * (fico - 695) - 0.45 * (np.log(annual_inc) - 11)
          + 0.0065 * (revol_util - 52) + 0.30 * (term == 60) + 0.11 * inq + 0.14 * delinq + 0.10 * pub
          + 0.45 * (purpose == "small_business") + 0.15 * (home == "RENT") - 0.12 * (emp >= 8)
          + 0.25 * np.log(loan_amnt / 10000) + 0.04 * (months / 12.0)
          + rng.normal(0, 0.6, n))                                        # unobservable heterogeneity
    c = brentq(lambda c: expit(z0 + c).mean() - target_bad_rate, -10, 10)
    p_true = expit(z0 + c)
    latent_bad = rng.random(n) < p_true

    # inject realistic missingness AFTER generating outcomes
    emp[rng.random(n) < 0.06] = np.nan
    dti[rng.random(n) < 0.004] = np.nan
    revol_util[rng.random(n) < 0.002] = np.nan

    resolved = issue < pd.Timestamp(resolved_before)
    as_of_ts = pd.Timestamp(as_of)
    status = np.where(resolved, np.where(latent_bad, "Charged Off", "Fully Paid"), "Current")
    elapsed = np.clip(((as_of_ts - issue).days / 30.4375).to_numpy(), 0, None)
    out_prncp = np.where(status == "Current", np.maximum(loan_amnt * (1 - elapsed / term), 0.05 * loan_amnt), 0.0)
    chg_bal = loan_amnt * rng.uniform(0.4, 0.9, n)
    rec_rate = rng.beta(2, 6, n)                                          # mean 0.25 -> LGD ~ 0.75 (unsecured)
    recoveries = np.where(status == "Charged Off", chg_bal * rec_rate, 0.0)
    max_dpd = np.where(status == "Charged Off", rng.choice([120, 150, 180], n),
                       np.where((status == "Current") & (rng.random(n) < 0.03), rng.integers(1, 60, n), 0))

    int_rate = np.round(5.5 + 28 * p_true ** 0.55 + rng.normal(0, 0.8, n), 2)
    g = pd.qcut(int_rate, 7, labels=list("ABCDEFG")).astype(str)
    sub = np.array([a + str(b) for a, b in zip(g, rng.integers(1, 6, n))])

    return pd.DataFrame({
        "loan_id": np.arange(1, n + 1), "issue_d": issue.normalize(), "loan_amnt": loan_amnt, "term": term,
        "int_rate": int_rate, "grade": g, "sub_grade": sub, "annual_inc": annual_inc.round(2), "dti": dti,
        "revol_util": revol_util.round(1), "revol_bal": revol_bal.round(2), "fico": fico, "emp_length": emp,
        "home_ownership": home, "purpose": purpose, "verification_status": verif, "inq_last_6mths": inq,
        "delinq_2yrs": delinq, "open_acc": open_acc, "total_acc": total_acc, "pub_rec": pub,
        "loan_status": status, "max_dpd": max_dpd, "out_prncp": out_prncp.round(2),
        "recoveries": recoveries.round(2), "_latent_bad": latent_bad.astype(int)})


def _emp_len(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip()
    out = pd.Series(np.nan, index=s.index, dtype=float)
    out[s.str.startswith("<", na=False)] = 0.0
    m = s.str.extract(r"(\d+)")[0].astype(float)
    return out.where(out.notna(), m)


def load_lendingclub(path: str) -> pd.DataFrame:
    """
    Maps the Kaggle 'accepted_2007_to_2018' file to our schema. NOT executed in the build sandbox (no
    Kaggle access) - validate column names against your download. Training population = Fully Paid vs
    Charged Off/Default; 'Current', 'Late', 'In Grace Period' are indeterminate and excluded from training
    (Current is kept as the live book to be scored).
    """
    cols = ["id", "issue_d", "loan_amnt", "term", "int_rate", "grade", "sub_grade", "annual_inc", "dti",
            "revol_util", "revol_bal", "fico_range_low", "fico_range_high", "emp_length", "home_ownership",
            "purpose", "verification_status", "inq_last_6mths", "delinq_2yrs", "open_acc", "total_acc",
            "pub_rec", "loan_status", "out_prncp", "recoveries"]
    raw = pd.read_csv(path, usecols=lambda c: c in cols, low_memory=False)
    raw = raw[pd.to_numeric(raw["id"], errors="coerce").notna()].copy()    # file ends with 2 footer rows
    df = pd.DataFrame({"loan_id": pd.to_numeric(raw["id"]).astype("int64")})
    df["issue_d"] = pd.to_datetime(raw["issue_d"], format="%b-%Y")
    df["loan_amnt"] = pd.to_numeric(raw["loan_amnt"])
    df["term"] = raw["term"].astype(str).str.extract(r"(\d+)")[0].astype(float)
    df["int_rate"] = pd.to_numeric(raw["int_rate"].astype(str).str.rstrip("%"), errors="coerce")
    df["grade"], df["sub_grade"] = raw["grade"], raw["sub_grade"]
    for c in ["annual_inc", "dti", "revol_bal", "inq_last_6mths", "delinq_2yrs", "open_acc", "total_acc",
              "pub_rec", "out_prncp", "recoveries"]:
        df[c] = pd.to_numeric(raw[c], errors="coerce")
    df["revol_util"] = pd.to_numeric(raw["revol_util"].astype(str).str.rstrip("%"), errors="coerce")
    df["fico"] = (pd.to_numeric(raw["fico_range_low"]) + pd.to_numeric(raw["fico_range_high"])) / 2
    df["emp_length"] = _emp_len(raw["emp_length"])
    for c in CATEGORICAL:
        df[c] = raw[c]
    s = raw["loan_status"].astype(str)
    bad = s.isin(["Charged Off", "Default", "Does not meet the credit policy. Status:Charged Off"])
    good = s.isin(["Fully Paid", "Does not meet the credit policy. Status:Fully Paid"])
    df["loan_status"] = np.select([bad & (s == "Default"), bad, good, s == "Current"],
                                  ["Default", "Charged Off", "Fully Paid", "Current"], default="Other")
    df["max_dpd"] = np.select([s == "Charged Off", s == "Default", s.str.startswith("Late (31-120"),
                               s.str.startswith("Late (16-30")], [150, 120, 60, 20], default=0)
    return df


def resolved_view(df: pd.DataFrame) -> pd.DataFrame:
    """Training population with target. bad = Charged Off / Default / >= 90 DPD ; good = Fully Paid."""
    r = df[df["loan_status"].isin(["Fully Paid", "Charged Off", "Default"])].copy()
    r["bad"] = ((r["loan_status"].isin(["Charged Off", "Default"])) | (r["max_dpd"] >= 90)).astype(int)
    return r.sort_values("issue_d").reset_index(drop=True)


# ---------------------------------------------------------------------------- warehouse export
def export_star(df: pd.DataFrame, outdir: str, as_of: str = "2019-03-31") -> None:
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)
    # LendingClub exposes no borrower id: borrower_id == loan_id (1:1). Repeat borrowers are invisible.
    pd.DataFrame({"borrower_id": df.loan_id, "age": np.nan, "housing_status": df.home_ownership,
                  "employment_length": df.emp_length, "annual_income": df.annual_inc,
                  "dti_ratio": df.dti}).to_csv(out / "dim_borrower.csv", index=False)
    pd.DataFrame({"loan_id": df.loan_id, "borrower_id": df.loan_id, "issue_date": df.issue_d.dt.date,
                  "loan_amount": df.loan_amnt, "interest_rate": df.int_rate, "grade": df.grade,
                  "sub_grade": df.sub_grade, "loan_status": df.loan_status, "repayment_term": df.term.astype(int)}
                 ).to_csv(out / "fact_loan_portfolio.csv", index=False)
    pd.DataFrame({"performance_id": np.arange(1, len(df) + 1), "loan_id": df.loan_id, "as_of_date": as_of,
                  "days_past_due": df.max_dpd.astype(int), "revolving_utilization": df.revol_util,
                  "delinquency_2yrs": df.delinq_2yrs, "revolving_balance": df.revol_bal,
                  "outstanding_principal": df.out_prncp, "recovery_amount": df.recoveries}
                 ).to_csv(out / "fact_credit_performance.csv", index=False)
