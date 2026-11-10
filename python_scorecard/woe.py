"""
Weight-of-Evidence binning.

Convention (matches the project spec): event = BAD (y == 1).
    WoE_i = ln( %good_i / %bad_i )          -> HIGH WoE = LOWER risk
    IV    = sum_i (%good_i - %bad_i) * WoE_i
Because WoE is oriented good/bad, a logistic regression that predicts BAD will have NEGATIVE
coefficients on every WoE feature. The sign constraint in model.py enforces coef <= 0.

Robustness rules (each one is covered by tests/test_woe.py):
  * Laplace smoothing (+0.5 per bin) so an empty good/bad cell never yields ln(0) or division by zero.
  * Bins with zero goods or zero bads, or smaller than `min_bin_pct`, are merged into a neighbour.
  * Numeric bins are forced monotonic in bad-rate (direction inferred from Spearman sign).
  * Missing values get their own bin; if that bin is tiny (< `min_missing_pct`) its WoE is set to 0 (neutral)
    rather than letting 8 observations produce an extreme WoE.
  * Unseen categories at scoring time map to WoE 0 (neutral) and bin index -1.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

MISSING = "__MISSING__"


class WoEBinner:
    def __init__(self, name: str, kind: str = "numeric", max_prebins: int = 20, min_bin_pct: float = 0.05,
                 monotonic: bool = True, min_missing_pct: float = 0.005, smoothing: float = 0.5):
        assert kind in ("numeric", "categorical")
        self.name, self.kind = name, kind
        self.max_prebins, self.min_bin_pct = max_prebins, min_bin_pct
        self.monotonic, self.min_missing_pct, self.smoothing = monotonic, min_missing_pct, smoothing
        self.fitted_ = False

    # ------------------------------------------------------------------ fit
    def fit(self, x: pd.Series, y) -> "WoEBinner":
        y = np.asarray(y).astype(int)
        if len(x) != len(y):
            raise ValueError("x and y length mismatch")
        if set(np.unique(y)) - {0, 1}:
            raise ValueError("y must be binary 0/1")
        if y.sum() == 0 or y.sum() == len(y):
            raise ValueError("y contains a single class; WoE is undefined")
        x = pd.Series(x).reset_index(drop=True)
        if self.kind == "numeric":
            self._fit_numeric(pd.to_numeric(x, errors="coerce"), y)
        else:
            self._fit_categorical(x, y)
        self.fitted_ = True
        return self

    def _fit_numeric(self, x: pd.Series, y: np.ndarray):
        miss = x.isna().to_numpy()
        xs, ys = x[~miss].to_numpy(float), y[~miss]
        n_valid = len(xs)
        self.n_total_ = len(x)
        if n_valid == 0 or np.unique(xs).size < 2:
            self.edges_ = np.array([])                      # single bin: carries no information
            tot, bad = np.array([n_valid]), np.array([ys.sum()])
        else:
            qs = np.linspace(0, 1, self.max_prebins + 1)[1:-1]
            edges = np.unique(np.quantile(xs, qs))
            # an edge equal to the max would create an empty last bin
            edges = edges[edges < xs.max()]
            idx = np.searchsorted(edges, xs, side="left")   # bin i holds edges[i-1] < x <= edges[i]
            k = len(edges) + 1
            tot = np.bincount(idx, minlength=k).astype(float)
            bad = np.bincount(idx, weights=ys, minlength=k).astype(float)
            rho = spearmanr(xs, ys).statistic if np.unique(ys).size > 1 else 0.0
            direction = 1 if (rho is None or np.isnan(rho) or rho >= 0) else -1
            edges, tot, bad = self._merge(edges, tot, bad, n_valid, direction)
            self.edges_ = edges
        self.direction_ = direction if n_valid and np.unique(xs).size > 1 else 0
        self._finalise(tot, bad, miss_tot=float(miss.sum()), miss_bad=float(y[miss].sum()))

    def _merge(self, edges, tot, bad, n_valid, direction):
        edges, tot, bad = list(edges), tot.copy(), bad.copy()

        def merge_pair(a):  # merge bins a and a+1
            tot[a] += tot[a + 1]; bad[a] += bad[a + 1]
            tot_l = np.delete(tot, a + 1); bad_l = np.delete(bad, a + 1)
            del edges[a]
            return tot_l, bad_l

        while len(tot) > 1:
            rate = bad / np.maximum(tot, 1)
            degenerate = np.where((bad == 0) | (tot - bad == 0) | (tot < self.min_bin_pct * n_valid))[0]
            if degenerate.size:
                i = int(degenerate[np.argmin(tot[degenerate])])
                if i == 0: a = 0
                elif i == len(tot) - 1: a = i - 1
                else: a = i - 1 if abs(rate[i] - rate[i - 1]) <= abs(rate[i] - rate[i + 1]) else i
                tot, bad = merge_pair(a)
                continue
            if self.monotonic:
                viol = np.where(direction * np.diff(rate) < 0)[0]
                if viol.size:
                    tot, bad = merge_pair(int(viol[0]))
                    continue
            break
        return np.array(edges), tot, bad

    def _fit_categorical(self, x: pd.Series, y: np.ndarray):
        s = x.astype("object").where(x.notna(), MISSING).astype(str)
        self.n_total_ = len(s)
        df = pd.DataFrame({"c": s, "y": y}).groupby("c")["y"].agg(["size", "sum"])
        df["rate"] = df["sum"] / df["size"]
        df = df.sort_values("rate")
        cats = list(df.index)
        groups = [[c] for c in cats]
        tot = df["size"].to_numpy(float).copy(); bad = df["sum"].to_numpy(float).copy()
        n = tot.sum()
        while len(tot) > 1:
            rate = bad / np.maximum(tot, 1)
            degenerate = np.where((bad == 0) | (tot - bad == 0) | (tot < self.min_bin_pct * n))[0]
            if not degenerate.size:
                break
            i = int(degenerate[np.argmin(tot[degenerate])])
            if i == 0: a = 0
            elif i == len(tot) - 1: a = i - 1
            else: a = i - 1 if abs(rate[i] - rate[i - 1]) <= abs(rate[i] - rate[i + 1]) else i
            groups[a] = groups[a] + groups[a + 1]; del groups[a + 1]
            tot[a] += tot[a + 1]; bad[a] += bad[a + 1]
            tot = np.delete(tot, a + 1); bad = np.delete(bad, a + 1)
        self.groups_ = groups
        self.cat_to_bin_ = {c: i for i, g in enumerate(groups) for c in g}
        self.direction_ = 0
        self._finalise(tot, bad, miss_tot=0.0, miss_bad=0.0, labels=[" | ".join(g)[:60] for g in groups])

    def _finalise(self, tot, bad, miss_tot, miss_bad, labels=None):
        tot = np.asarray(tot, float); bad = np.asarray(bad, float)
        if labels is None:
            if self.kind == "numeric":
                e = self.edges_
                labels = ["(-inf, +inf)"] if e.size == 0 else (
                    [f"(-inf, {e[0]:.4g}]"] + [f"({e[i-1]:.4g}, {e[i]:.4g}]" for i in range(1, len(e))] + [f"({e[-1]:.4g}, +inf)"])
        has_missing = miss_tot > 0
        if has_missing:
            labels = list(labels) + ["Missing"]
            tot = np.append(tot, miss_tot); bad = np.append(bad, miss_bad)
        good = tot - bad
        k = len(tot)
        G, B = good.sum(), bad.sum()
        s = self.smoothing
        dg = (good + s) / (G + s * k)
        db = (bad + s) / (B + s * k)
        woe = np.log(dg / db)
        if has_missing and miss_tot < self.min_missing_pct * self.n_total_:
            woe[-1] = 0.0                                   # tiny missing bin -> neutral
        self.bins_ = pd.DataFrame({
            "feature": self.name, "bin": np.arange(k), "label": labels, "count": tot.astype(int),
            "pct_total": tot / tot.sum(), "bad": bad.astype(int), "good": good.astype(int),
            "bad_rate": bad / np.maximum(tot, 1), "dist_good": dg, "dist_bad": db, "woe": woe,
            "iv_contrib": (dg - db) * woe})
        self.has_missing_ = has_missing
        self.iv_ = float(self.bins_["iv_contrib"].sum())

    # ------------------------------------------------------------------ transform
    def transform_bin(self, x: pd.Series) -> np.ndarray:
        self._check()
        x = pd.Series(x).reset_index(drop=True)
        if self.kind == "numeric":
            v = pd.to_numeric(x, errors="coerce").to_numpy(float)
            idx = np.searchsorted(self.edges_, v, side="left")
            if self.has_missing_:
                idx = np.where(np.isnan(v), len(self.bins_) - 1, idx)
            else:
                idx = np.where(np.isnan(v), -1, idx)       # missing never seen in training -> neutral
            return idx.astype(int)
        s = x.astype("object").where(x.notna(), MISSING).astype(str)
        return s.map(self.cat_to_bin_).fillna(-1).astype(int).to_numpy()

    def transform(self, x: pd.Series) -> np.ndarray:
        idx = self.transform_bin(x)
        woe = self.bins_["woe"].to_numpy()
        return np.where(idx >= 0, woe[np.clip(idx, 0, len(woe) - 1)], 0.0)

    def bin_distribution(self, x: pd.Series) -> np.ndarray:
        """Share of observations in each fitted bin (for characteristic PSI). Unseen -> ignored."""
        idx = self.transform_bin(x)
        idx = idx[idx >= 0]
        return np.bincount(idx, minlength=len(self.bins_)) / max(len(idx), 1)

    def is_monotonic(self) -> bool:
        if self.kind != "numeric":
            return True
        r = self.bins_.loc[~self.bins_["label"].eq("Missing"), "bad_rate"].to_numpy()
        d = np.diff(r)
        return bool(np.all(d >= -1e-12) or np.all(d <= 1e-12))

    def _check(self):
        if not self.fitted_:
            raise RuntimeError("binner not fitted")


def information_value_table(binners: dict) -> pd.DataFrame:
    return (pd.DataFrame([{"feature": n, "iv": b.iv_, "n_bins": len(b.bins_), "monotonic": b.is_monotonic()}
                          for n, b in binners.items()]).sort_values("iv", ascending=False).reset_index(drop=True))


def iv_strength(iv: float) -> str:
    if iv < 0.02: return "useless"
    if iv < 0.1: return "weak"
    if iv < 0.3: return "medium"
    if iv <= 0.5: return "strong"
    return "suspicious"
