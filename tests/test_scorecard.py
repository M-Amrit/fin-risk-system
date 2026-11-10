import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python_scorecard"))
from woe import WoEBinner                                           # noqa: E402
from model import (fit_bounded_logit, ScoreScaler, psi, psi_from_dist, make_band_edges, assign_band,  # noqa: E402
                   BAND_LABELS, build_points_table, neutral_points, score_with_neutral, validation_metrics)
import generate_ledger as GL                                        # noqa: E402

RNG = np.random.default_rng(0)


def _make(n=20000, noise_u_shape=False):
    x = RNG.normal(0, 1, n)
    z = -1.5 + 0.9 * x
    if noise_u_shape:                       # non-monotone relationship forces bin merging
        z = -1.5 + 0.9 * x ** 2 * np.sign(x) * 0 + 0.8 * np.abs(x)
    y = (RNG.random(n) < 1 / (1 + np.exp(-z))).astype(int)
    return pd.Series(x), y


# ------------------------------------------------------------------ WoE / IV
def test_numeric_bins_are_monotonic_even_for_u_shaped_signal():
    x, y = _make(noise_u_shape=True)
    b = WoEBinner("x").fit(x, y)
    assert b.is_monotonic()
    assert len(b.bins_) >= 1


def test_woe_orientation_high_bad_rate_means_low_woe():
    x, y = _make()
    b = WoEBinner("x").fit(x, y).bins_
    b = b[b.label != "Missing"]
    assert np.corrcoef(b.bad_rate, b.woe)[0, 1] < -0.95


def test_iv_matches_manual_formula_and_is_nonnegative():
    x, y = _make()
    b = WoEBinner("x").fit(x, y)
    t = b.bins_
    manual = ((t.dist_good - t.dist_bad) * np.log(t.dist_good / t.dist_bad)).sum()
    assert b.iv_ == pytest.approx(manual)
    assert (t.iv_contrib >= -1e-12).all() and b.iv_ > 0


def test_zero_event_category_does_not_divide_by_zero():
    n = 5000
    cat = pd.Series(np.where(np.arange(n) < 4900, "A", "B"))
    y = np.where(cat == "A", (np.arange(n) % 5 == 0).astype(int), 0)       # 'B' has zero events
    y[0] = 1
    b = WoEBinner("c", "categorical", min_bin_pct=0.001).fit(cat, y)
    assert np.isfinite(b.bins_.woe).all() and np.isfinite(b.iv_)


def test_perfectly_separable_bins_stay_finite():
    x = pd.Series(np.r_[np.zeros(500), np.ones(500) * 10.0])
    y = np.r_[np.zeros(500), np.ones(500)].astype(int)
    b = WoEBinner("x", min_bin_pct=0.01).fit(x, y)
    assert np.isfinite(b.transform(x)).all() and np.isfinite(b.iv_)


def test_single_class_target_raises_clear_error():
    with pytest.raises(ValueError, match="single class"):
        WoEBinner("x").fit(pd.Series(np.arange(100.0)), np.zeros(100, int))


def test_constant_feature_has_zero_iv():
    b = WoEBinner("x").fit(pd.Series(np.ones(1000)), np.r_[np.ones(200), np.zeros(800)].astype(int))
    assert b.iv_ == pytest.approx(0.0, abs=1e-12)


def test_missing_gets_own_bin_and_tiny_missing_is_neutral():
    x, y = _make(20000)
    x_big = x.copy(); x_big[:3000] = np.nan
    assert "Missing" in WoEBinner("x").fit(x_big, y).bins_.label.values
    x_tiny = x.copy(); x_tiny[:20] = np.nan                                   # 0.1% < 0.5% threshold
    t = WoEBinner("x").fit(x_tiny, y).bins_
    assert t[t.label == "Missing"].woe.iloc[0] == 0.0


def test_unseen_category_and_unseen_missing_map_to_neutral():
    x = pd.Series(["a", "b"] * 2000); y = np.r_[np.zeros(1000), np.ones(500), np.zeros(1000), np.ones(1500)][:4000]
    y = (np.arange(4000) % 7 == 0).astype(int) | ((x == "b") & (np.arange(4000) % 3 == 0)).astype(int)
    b = WoEBinner("c", "categorical").fit(x, y)
    assert b.transform(pd.Series(["zzz"]))[0] == 0.0
    xn, yn = _make(5000)
    bn = WoEBinner("x").fit(xn, yn)
    assert bn.transform(pd.Series([np.nan]))[0] == 0.0


def test_transform_is_deterministic_and_aligned_with_bins():
    x, y = _make()
    b = WoEBinner("x").fit(x, y)
    idx = b.transform_bin(x)
    assert idx.min() >= 0 and idx.max() < len(b.bins_)
    assert np.allclose(b.transform(x), b.bins_.woe.to_numpy()[idx])


# ------------------------------------------------------------------ model / scaling
def test_bounded_logit_never_returns_positive_slope():
    n = 5000
    x1 = RNG.normal(size=n)
    y = (RNG.random(n) < 1 / (1 + np.exp(-(-1 + 0.8 * x1)))).astype(int)      # POSITIVE relationship
    b0, b, ok = fit_bounded_logit(x1.reshape(-1, 1), y)
    assert b[0] <= 1e-9                                                       # constrained to the bound


def test_bounded_logit_recovers_negative_slope():
    n = 20000
    x1 = RNG.normal(size=n)
    y = (RNG.random(n) < 1 / (1 + np.exp(-(-1 - 0.7 * x1)))).astype(int)
    b0, b, ok = fit_bounded_logit(x1.reshape(-1, 1), y, l2=0.0)
    assert ok and b[0] == pytest.approx(-0.7, abs=0.06) and b0 == pytest.approx(-1, abs=0.06)


def test_scaling_calibration_600_at_50_to_1_and_pdo_20():
    sc = ScoreScaler(600, 50, 20)
    assert sc.offset + sc.factor * np.log(50) == pytest.approx(600)
    assert sc.offset + sc.factor * np.log(100) == pytest.approx(620)          # doubling odds = +20 points
    assert sc.offset + sc.factor * np.log(25) == pytest.approx(580)
    assert sc.pd_from_score(600) == pytest.approx(1 / 51)                     # odds 50:1 good:bad -> PD 1/51
    assert sc.score_from_logodds_bad(np.log(1 / 50)) == pytest.approx(600)


def test_points_table_sums_to_scaled_logodds_within_rounding():
    n = 30000
    df = pd.DataFrame({"a": RNG.normal(size=n), "b": RNG.normal(size=n)})
    z = -1.6 + 0.7 * df.a + 0.5 * df.b
    y = (RNG.random(n) < 1 / (1 + np.exp(-z))).astype(int)
    binners = {c: WoEBinner(c).fit(df[c], y) for c in df}
    X = pd.DataFrame({c: binners[c].transform(df[c]) for c in df})
    b0, b, _ = fit_bounded_logit(X.to_numpy(), y)
    coefs = pd.Series(b, index=df.columns)
    sc = ScoreScaler()
    pts = build_points_table(binners, b0, coefs, sc)
    s = score_with_neutral(df, binners, pts, neutral_points(pts, sc, b0, 2))
    exact = sc.score_from_logodds_bad(b0 + X.to_numpy() @ b)
    assert np.max(np.abs(s - exact)) <= 1.0 + 1e-9                            # <= 0.5 rounding per feature, 2 features


def test_bands_cover_five_labels_and_bad_rate_falls_with_score():
    n = 60000
    s = RNG.normal(550, 40, n)
    y = (RNG.random(n) < 1 / (1 + np.exp((s - 520) / 25))).astype(int)
    edges = make_band_edges(s)
    bands = assign_band(s, edges)
    assert set(bands) == set(BAND_LABELS)
    rates = [y[bands == l].mean() for l in BAND_LABELS]
    assert all(np.diff(rates) < 0)


def test_gini_ks_identities():
    y = (RNG.random(20000) < 0.15).astype(int)
    p = np.clip(0.15 + 0.2 * (y - 0.15) + RNG.normal(0, 0.1, 20000), 0.001, 0.999)
    m = validation_metrics(y, p)
    assert m["gini"] == pytest.approx(2 * m["auc"] - 1)
    assert 0 < m["ks"] < 1 and sum(map(sum, m["confusion_matrix"])) == len(y)


# ------------------------------------------------------------------ PSI
def test_psi_identical_is_zero_and_shift_is_large():
    a = RNG.normal(0, 1, 50000)
    assert psi(a, a.copy()) == pytest.approx(0.0, abs=1e-9)
    assert psi(a, RNG.normal(1.5, 1, 50000)) > 0.25


def test_psi_empty_bin_is_finite():
    v = psi_from_dist(np.array([0.5, 0.5, 0.0]), np.array([0.0, 0.5, 0.5]))
    assert np.isfinite(v) and v > 0


# ------------------------------------------------------------------ ledger generator
def test_generated_ledger_balances_per_batch_and_dates_match_period():
    coa, dept, gl, bud = GL.build("2023-01", 14, 1, 1e7, 3e7, 0.02, 0.13, 0.045, 0.045, split=3)
    g = gl.groupby("entry_batch_id")[["debit_amount", "credit_amount"]].sum()
    assert (g.debit_amount - g.credit_amount).abs().max() < 0.005
    td = pd.to_datetime(gl.transaction_date)
    assert (td.dt.to_period("M").dt.to_timestamp().dt.date == gl.posting_period).all()    # incl. February
    assert ((gl.debit_amount > 0) & (gl.credit_amount > 0)).sum() == 0
