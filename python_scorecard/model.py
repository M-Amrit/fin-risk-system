"""
Sign-constrained logistic regression, score scaling, banding, validation metrics, PSI.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit
from sklearn.metrics import (roc_auc_score, roc_curve, confusion_matrix, classification_report)


# --------------------------------------------------------------------------- model
def fit_bounded_logit(X: np.ndarray, y: np.ndarray, upper: float = 0.0, l2: float = 1e-3):
    """
    Maximum-likelihood logistic regression of P(bad) on WoE features with every slope <= `upper` (0).
    WoE is oriented ln(good/bad), so a legitimate risk relationship has a NEGATIVE slope; a positive
    unconstrained slope signals multicollinearity or a bin-ordering problem, not a real effect.
    Intercept is unconstrained. Light L2 (on slopes only) keeps the problem well-posed under separation.
    Returns (intercept, slopes, converged).
    """
    n, k = X.shape
    A = np.hstack([np.ones((n, 1)), X])

    def f(b):
        z = A @ b
        nll = np.sum(np.logaddexp(0, z) - y * z) / n
        return nll + 0.5 * l2 * np.sum(b[1:] ** 2)

    def g(b):
        r = (expit(A @ b) - y)
        grad = A.T @ r / n
        grad[1:] += l2 * b[1:]
        return grad

    p0 = np.clip(y.mean(), 1e-6, 1 - 1e-6)
    b0 = np.zeros(k + 1); b0[0] = np.log(p0 / (1 - p0))
    bounds = [(None, None)] + [(None, upper)] * k
    res = minimize(f, b0, jac=g, method="L-BFGS-B", bounds=bounds, options={"maxiter": 2000, "ftol": 1e-12})
    return float(res.x[0]), res.x[1:].copy(), bool(res.success)


def fit_with_pruning(X: pd.DataFrame, y: np.ndarray, tol: float = 1e-6, **kw):
    """Iteratively drop features whose constrained slope is pinned at the bound (~0), then refit."""
    cols = list(X.columns)
    dropped = []
    while True:
        b0, b, ok = fit_bounded_logit(X[cols].to_numpy(), y, **kw)
        pinned = [c for c, v in zip(cols, b) if v > -tol]
        if not pinned or len(cols) - len(pinned) < 1:
            return b0, pd.Series(b, index=cols), ok, dropped
        dropped += pinned
        cols = [c for c in cols if c not in pinned]


# --------------------------------------------------------------------------- scaling
class ScoreScaler:
    """Score = Offset + Factor * ln(odds_good_to_bad). Spec: 600 points at 50:1, PDO = 20."""
    def __init__(self, base_score=600.0, base_odds=50.0, pdo=20.0):
        self.base_score, self.base_odds, self.pdo = base_score, base_odds, pdo
        self.factor = pdo / np.log(2)
        self.offset = base_score - self.factor * np.log(base_odds)

    def score_from_logodds_bad(self, z):          # z = ln(bad/good) from the LR
        return self.offset - self.factor * np.asarray(z)

    def pd_from_score(self, score):               # inverse mapping
        return expit(-(np.asarray(score, float) - self.offset) / self.factor)

    def odds_from_score(self, score):
        return np.exp((np.asarray(score, float) - self.offset) / self.factor)


def build_points_table(binners: dict, intercept: float, coefs: pd.Series, scaler: ScoreScaler) -> pd.DataFrame:
    """
    points_ij = -Factor * beta_j * WoE_ij + (Offset - Factor * beta_0) / n_features   (rounded to int)
    Scoring with the ROUNDED points is what an auditor can reproduce by hand; PD is then derived from
    that integer score so the score->PD mapping is exactly consistent.
    """
    n = len(coefs)
    base = (scaler.offset - scaler.factor * intercept) / n
    rows = []
    for f, beta in coefs.items():
        t = binners[f].bins_[["feature", "bin", "label", "count", "bad_rate", "woe"]].copy()
        t["coef"] = beta
        t["points"] = np.round(-scaler.factor * beta * t["woe"] + base).astype(int)
        rows.append(t)
    return pd.concat(rows, ignore_index=True)


def neutral_points(points: pd.DataFrame, scaler: ScoreScaler, intercept: float, n_features: int) -> float:
    """Points awarded for an unseen/neutral (WoE = 0) value: just the distributed base points."""
    return float(np.round((scaler.offset - scaler.factor * intercept) / n_features))


def score_with_neutral(df, binners, points, neutral_pts):
    total = np.zeros(len(df))
    for f, grp in points.groupby("feature", sort=False):
        idx = binners[f].transform_bin(df[f])
        pts = grp.sort_values("bin")["points"].to_numpy()
        total += np.where(idx >= 0, pts[np.clip(idx, 0, len(pts) - 1)], neutral_pts)
    return total


# --------------------------------------------------------------------------- banding
BAND_LABELS = ["Very High Risk", "High Risk", "Medium Risk", "Low Risk", "Very Low Risk"]  # ascending score


def make_band_edges(train_scores: np.ndarray, n_bands: int = 5) -> np.ndarray:
    """Equal-population cut-offs on the TRAIN score distribution (4 interior edges for 5 bands)."""
    return np.quantile(train_scores, np.linspace(0, 1, n_bands + 1)[1:-1])


def assign_band(scores: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.array(BAND_LABELS, dtype=object)[np.searchsorted(edges, scores, side="right")]


def band_summary(scores, y, pd_pred, edges, label="") -> pd.DataFrame:
    band = assign_band(scores, edges)
    df = pd.DataFrame({"band": pd.Categorical(band, categories=BAND_LABELS, ordered=True), "y": y,
                       "pd": pd_pred, "score": scores})
    out = df.groupby("band", observed=False).agg(n=("y", "size"), bads=("y", "sum"), observed_pd=("y", "mean"),
                                                 mean_predicted_pd=("pd", "mean"), min_score=("score", "min"),
                                                 max_score=("score", "max")).reset_index()
    out.insert(0, "sample", label)
    return out


# --------------------------------------------------------------------------- metrics
def ks_statistic(y, p):
    fpr, tpr, thr = roc_curve(y, p)
    d = tpr - fpr
    i = int(np.argmax(d))
    return float(d[i]), float(thr[i])


def validation_metrics(y, p, cutoff=None) -> dict:
    auc = roc_auc_score(y, p)
    ks, ks_thr = ks_statistic(y, p)
    thr = ks_thr if cutoff is None else cutoff
    pred = (p >= thr).astype(int)
    return {"auc": float(auc), "gini": float(2 * auc - 1), "ks": ks, "cutoff": float(thr),
            "confusion_matrix": confusion_matrix(y, pred, labels=[0, 1]).tolist(),
            "report": classification_report(y, pred, target_names=["good", "bad"], digits=3, zero_division=0)}


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10, eps: float = 1e-4) -> float:
    """PSI with quantile bins fitted on `expected`. eps floors empty bins so ln(0) / x/0 cannot occur."""
    expected, actual = np.asarray(expected, float), np.asarray(actual, float)
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)[1:-1]))
    e = np.bincount(np.searchsorted(edges, expected, side="left"), minlength=len(edges) + 1) / len(expected)
    a = np.bincount(np.searchsorted(edges, actual, side="left"), minlength=len(edges) + 1) / len(actual)
    return psi_from_dist(e, a, eps)


def psi_from_dist(e: np.ndarray, a: np.ndarray, eps: float = 1e-4) -> float:
    e, a = np.clip(np.asarray(e, float), eps, None), np.clip(np.asarray(a, float), eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def psi_flag(v: float) -> str:
    return "stable" if v < 0.10 else ("monitor" if v < 0.25 else "UNSTABLE (>0.25)")
