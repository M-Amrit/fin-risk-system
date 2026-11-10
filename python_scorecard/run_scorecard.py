"""
End-to-end credit scorecard pipeline.

  python run_scorecard.py --synthetic 200000 --out ../data/scorecard_out --export-star ../data/synthetic
  python run_scorecard.py --lendingclub /path/accepted_2007_to_2018.csv --out ../data/scorecard_out

Split policy: OUT-OF-TIME. Earliest 70% of resolved loans (by issue date) train, latest 30% test.
A random split on LendingClub-style data overstates performance because underwriting policy drifts.
"""
import argparse, json, sys, warnings
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.outliers_influence import variance_inflation_factor

sys.path.insert(0, str(Path(__file__).parent))
from woe import WoEBinner, information_value_table, iv_strength
from model import (fit_with_pruning, ScoreScaler, build_points_table, neutral_points, score_with_neutral,
                   make_band_edges, assign_band, band_summary, validation_metrics, ks_statistic, psi, psi_flag,
                   psi_from_dist)
import data as D

warnings.filterwarnings("ignore", category=FutureWarning)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--synthetic", type=int, metavar="N")
    g.add_argument("--lendingclub", type=str, metavar="CSV")
    ap.add_argument("--out", default="../data/scorecard_out")
    ap.add_argument("--export-star", default=None, help="dir to write warehouse CSVs")
    ap.add_argument("--as-of", default="2019-03-31")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--iv-min", type=float, default=0.02)
    ap.add_argument("--iv-max", type=float, default=0.50)
    ap.add_argument("--corr-max", type=float, default=0.70)
    ap.add_argument("--test-frac", type=float, default=0.30)
    ap.add_argument("--include-leaky", action="store_true", help="DEMO ONLY: add int_rate/grade to show leakage")
    ap.add_argument("--lgd-fallback", type=float, default=0.75)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    full = D.synthetic_loans(a.synthetic, seed=a.seed, as_of=a.as_of) if a.synthetic else D.load_lendingclub(a.lendingclub)
    res = D.resolved_view(full)
    live = full[full.loan_status == "Current"].copy()
    print(f"loans={len(full):,} resolved={len(res):,} (bad rate {res.bad.mean():.2%}) live={len(live):,}")

    # ---------------- out-of-time split
    cut = int(len(res) * (1 - a.test_frac))
    train, test = res.iloc[:cut].copy(), res.iloc[cut:].copy()
    print(f"train {train.issue_d.min():%Y-%m}..{train.issue_d.max():%Y-%m} n={len(train):,} bad={train.bad.mean():.2%} | "
          f"test {test.issue_d.min():%Y-%m}..{test.issue_d.max():%Y-%m} n={len(test):,} bad={test.bad.mean():.2%}")

    # ---------------- binning + IV
    num = list(D.NUMERIC) + (["int_rate"] if a.include_leaky else [])
    cat = list(D.CATEGORICAL) + (["grade"] if a.include_leaky else [])
    binners = {}
    for f in num:
        binners[f] = WoEBinner(f, "numeric").fit(train[f], train.bad)
    for f in cat:
        binners[f] = WoEBinner(f, "categorical").fit(train[f], train.bad)
    ivt = information_value_table(binners)
    ivt["strength"] = ivt.iv.map(iv_strength)
    keep = ivt[(ivt.iv >= a.iv_min) & (ivt.iv <= a.iv_max)].feature.tolist()
    ivt["selected_by_iv"] = ivt.feature.isin(keep)
    print("\nInformation Value:\n", ivt.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    def woe_matrix(df, feats):
        return pd.DataFrame({f: binners[f].transform(df[f]) for f in feats}, index=df.index)

    Xtr_all = woe_matrix(train, keep)
    # multicollinearity filter on WoE correlation: keep the higher-IV feature of any pair above corr-max
    iv_map = dict(zip(ivt.feature, ivt.iv)); dropped_corr = []
    changed = True
    while changed:
        changed = False
        c = Xtr_all.corr().abs().where(lambda m: np.triu(np.ones(m.shape), 1).astype(bool))
        pairs = c.stack()
        pairs = pairs[pairs > a.corr_max]
        if len(pairs):
            f1, f2 = pairs.idxmax()
            drop = f1 if iv_map[f1] < iv_map[f2] else f2
            Xtr_all = Xtr_all.drop(columns=drop); dropped_corr.append(drop); changed = True
    feats = list(Xtr_all.columns)
    print(f"\ndropped for |corr|>{a.corr_max}: {dropped_corr}")

    # ---------------- constrained logistic regression
    b0, coefs, converged, dropped_pinned = fit_with_pruning(Xtr_all, train.bad.to_numpy())
    feats = list(coefs.index)
    print(f"constrained LR converged={converged}; dropped (coef pinned at 0): {dropped_pinned}")
    print("coefficients (all must be <= 0):\n", coefs.round(4).to_string())

    # unconstrained statsmodels fit for p-values and VIF (diagnostic only; the scorecard uses the constrained fit)
    Xc = sm.add_constant(Xtr_all[feats])
    sm_fit = sm.Logit(train.bad.to_numpy(), Xc).fit(disp=0)
    vif = pd.Series([variance_inflation_factor(Xc.values, i) for i in range(1, Xc.shape[1])], index=feats)
    (out / "model_summary.txt").write_text(str(sm_fit.summary()) + "\n\nVIF\n" + vif.round(3).to_string())
    pos = sm_fit.params.drop("const")
    if (pos > 0).any():
        print("WARNING: unconstrained fit has positive slopes on", list(pos[pos > 0].index))

    # ---------------- scaling, scoring
    sc = ScoreScaler(600, 50, 20)
    points = build_points_table(binners, b0, coefs, sc)
    neutral = neutral_points(points, sc, b0, len(feats))
    s_tr = score_with_neutral(train, binners, points, neutral)
    s_te = score_with_neutral(test, binners, points, neutral)
    p_tr, p_te = sc.pd_from_score(s_tr), sc.pd_from_score(s_te)
    print(f"\nscaling: factor={sc.factor:.4f} offset={sc.offset:.4f}  | check score(odds 50)={sc.offset + sc.factor*np.log(50):.1f} "
          f"score(odds 100)={sc.offset + sc.factor*np.log(100):.1f}")

    # ---------------- bands
    edges = make_band_edges(s_tr, 5)
    bands = pd.concat([band_summary(s_tr, train.bad.to_numpy(), p_tr, edges, "train"),
                       band_summary(s_te, test.bad.to_numpy(), p_te, edges, "test")], ignore_index=True)
    print("\nRisk bands:\n", bands.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    te_rates = bands[bands["sample"] == "test"].observed_pd.to_numpy()
    band_monotone = bool(np.all(np.diff(te_rates) <= 1e-12))   # Very High Risk first -> rates must fall

    # ---------------- validation
    m_tr = validation_metrics(train.bad.to_numpy(), p_tr)
    m_te = validation_metrics(test.bad.to_numpy(), p_te, cutoff=m_tr["cutoff"])   # cutoff fitted on TRAIN
    print(f"\nTRAIN  AUC={m_tr['auc']:.4f} Gini={m_tr['gini']:.4f} KS={m_tr['ks']:.4f}")
    print(f"TEST   AUC={m_te['auc']:.4f} Gini={m_te['gini']:.4f} KS={ks_statistic(test.bad.to_numpy(), p_te)[0]:.4f}"
          f"  (cutoff PD={m_tr['cutoff']:.4f} from train)")
    print("confusion matrix [[TN, FP],[FN, TP]] (test):", m_te["confusion_matrix"])
    print(m_te["report"])
    calib = {"test_mean_pd": float(p_te.mean()), "test_observed_bad_rate": float(test.bad.mean())}
    print(f"calibration (test): predicted {calib['test_mean_pd']:.4f} vs observed {calib['test_observed_bad_rate']:.4f}")

    # ---------------- stability
    psi_score = psi(s_tr, s_te)
    char = []
    for f in feats:
        char.append({"feature": f, "psi": psi_from_dist(binners[f].bin_distribution(train[f]),
                                                          binners[f].bin_distribution(test[f]))})
    char = pd.DataFrame(char).sort_values("psi", ascending=False)
    char["flag"] = char.psi.map(psi_flag)
    print(f"\nscore PSI train->test = {psi_score:.4f} ({psi_flag(psi_score)})\n", char.to_string(index=False))

    # ---------------- live book -> expected loss
    if len(live):
        s_lv = score_with_neutral(live, binners, points, neutral)
        p_lv = np.clip(sc.pd_from_score(s_lv), 1e-6, 1 - 1e-6)
        bad_loans = train[train.bad == 1]
        has_rec = "recoveries" in train and train.recoveries.sum() > 0
        lgd = float(np.clip(1 - bad_loans.recoveries.sum() / bad_loans.loan_amnt.sum(), 0, 1)) if has_rec else a.lgd_fallback
        stg = pd.DataFrame({"loan_id": live.loan_id.to_numpy(), "as_of_date": a.as_of, "score": s_lv.astype(int),
                            "pd": p_lv.round(8), "risk_band": assign_band(s_lv, edges),
                            "ead": live.out_prncp.to_numpy().round(2), "lgd": round(lgd, 4)})
        stg.to_csv(out / "stg_scored_loans.csv", index=False)
        el = (stg.pd * stg.lgd * stg.ead)
        print(f"\nLIVE book n={len(stg):,} EAD={stg.ead.sum():,.0f} LGD={lgd:.3f} (from train recoveries/loan_amnt; conservative) "
              f"EL={el.sum():,.0f} ({el.sum()/stg.ead.sum():.2%} of EAD) score PSI vs train={psi(s_tr, s_lv):.4f}")
        print("NOTE: PD is the model's resolved-loan (lifetime-style) default probability, NOT a 12-month PD.")

    # ---------------- persist
    ivt.to_csv(out / "iv_table.csv", index=False)
    points.to_csv(out / "scorecard_points.csv", index=False)
    bands.to_csv(out / "band_summary.csv", index=False)
    char.to_csv(out / "characteristic_psi.csv", index=False)
    pd.concat([b.bins_ for b in binners.values()]).to_csv(out / "woe_bins_all.csv", index=False)
    summary = {"n_train": len(train), "n_test": len(test), "features": feats, "dropped_corr": dropped_corr,
               "dropped_pinned": dropped_pinned, "intercept": b0, "coefs": coefs.to_dict(),
               "factor": sc.factor, "offset": sc.offset, "band_edges": edges.tolist(),
               "band_test_bad_rate_monotone": band_monotone,
               "train": {k: m_tr[k] for k in ("auc", "gini", "ks")},
               "test": {"auc": m_te["auc"], "gini": m_te["gini"], "ks": ks_statistic(test.bad.to_numpy(), p_te)[0],
                        "cutoff": m_tr["cutoff"], "confusion_matrix": m_te["confusion_matrix"]},
               "calibration": calib, "score_psi": psi_score}
    (out / "metrics.json").write_text(json.dumps(summary, indent=2, default=float))
    pd.DataFrame([{"score_psi": psi_score, "test_auc": m_te["auc"], "test_gini": m_te["gini"],
                   "test_ks": summary["test"]["ks"], "train_gini": m_tr["gini"],
                   "n_features": len(feats)}]).to_csv(out / "model_metrics_flat.csv", index=False)
    joblib.dump({"binners": binners, "points": points, "neutral": neutral, "scaler": sc, "edges": edges,
                 "features": feats}, out / "scorecard_model.joblib")
    if a.export_star:
        D.export_star(full[full.loan_status.isin(["Current", "Fully Paid", "Charged Off", "Default"])],
                      a.export_star, a.as_of)
        print(f"warehouse CSVs -> {a.export_star}")
    print(f"\nartifacts -> {out.resolve()}")


if __name__ == "__main__":
    main()
