"""
aggregation/compare.py
-----------------------
Stage 9 — Result aggregation and validation metrics.

Inputs
------
  ore/outputs/<exp>/npv_sabr.csv
  ore/outputs/<exp>/npv_localvol.csv

Outputs written to aggregation/<exp>/
  comparison_metrics.csv    – merged table with per-trade error metrics
  summary_by_product.csv    – grouped stats (mean / std / max error)
  validation_summary.json   – overall pass/fail plus key stats
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Validation thresholds -------------------------------------------------
CONSISTENCY_THRESHOLD = 0.10   # |ModelError| must be below this to PASS
BARRIER_THRESHOLD     = 0.20   # looser for path-dependent trades
# -----------------------------------------------------------------------


def relative_model_error(npv_sabr: float, npv_lv: float, vega: float) -> float:
    """
    Vega-normalised model error:    (NPV_SABR − NPV_LV) / |Vega_SABR|

    Falls back to absolute price difference when vega is near zero.
    """
    if abs(vega) > 1e-6:
        return (npv_sabr - npv_lv) / abs(vega)
    if abs(npv_sabr) > 1e-6:
        return (npv_sabr - npv_lv) / abs(npv_sabr)
    return npv_sabr - npv_lv


def compare_results(
    sabr_csv: Path,
    lv_csv: Path,
    output_dir: Path,
) -> dict:
    """
    Merge SABR and Local Vol pricing results, compute validation metrics.

    Parameters
    ----------
    sabr_csv   : Path to npv_sabr.csv.
    lv_csv     : Path to npv_localvol.csv.
    output_dir : Where to write aggregation outputs.

    Returns
    -------
    validation_summary dict.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sabr_df = pd.read_csv(sabr_csv)
    lv_df   = pd.read_csv(lv_csv)

    # Merge on TradeId
    df = sabr_df.merge(
        lv_df[["TradeId", "NPV", "MC_StdErr", "PricingTime_s"]],
        on="TradeId",
        suffixes=("_SABR", "_LV"),
    )

    # Core metrics
    df["AbsDiff"]     = (df["NPV_SABR"] - df["NPV_LV"]).abs()
    df["RelDiff"]     = df["AbsDiff"] / (df["NPV_SABR"].abs().clip(lower=1e-8))
    df["ModelError"]  = df.apply(
        lambda r: relative_model_error(r["NPV_SABR"], r["NPV_LV"], r.get("Vega", 0.0)),
        axis=1,
    )
    df["Passed"] = df.apply(
        lambda r: abs(r["ModelError"]) < (
            BARRIER_THRESHOLD if r["ProductType"] in {"Barrier", "Asian", "ForwardStart"}
            else CONSISTENCY_THRESHOLD
        ),
        axis=1,
    )

    comparison_csv = output_dir / "comparison_metrics.csv"
    df.to_csv(comparison_csv, index=False)
    logger.info("Comparison metrics saved → %s", comparison_csv)

    # Per-product-type summary
    grp = df.groupby("ProductType")
    summary_rows = []
    for prod, g in grp:
        rmse   = math.sqrt((g["ModelError"] ** 2).mean())
        n_fail = int((~g["Passed"]).sum())
        summary_rows.append({
            "ProductType":  prod,
            "N":            len(g),
            "RMSE":         round(rmse, 6),
            "MeanError":    round(g["ModelError"].mean(), 6),
            "MaxAbsError":  round(g["ModelError"].abs().max(), 6),
            "StdError":     round(g["ModelError"].std(), 6),
            "N_Failed":     n_fail,
            "PassRate_%":   round(100 * (len(g) - n_fail) / len(g), 1),
        })
    summary_df = pd.DataFrame(summary_rows)
    summary_csv = output_dir / "summary_by_product.csv"
    summary_df.to_csv(summary_csv, index=False)
    logger.info("Product summary saved → %s", summary_csv)

    # Per-maturity RMSE for vanillas
    van_df = df[df["ProductType"] == "Vanilla"]
    mat_rmse = {}
    for lbl, g in van_df.groupby("Maturity"):
        mat_rmse[lbl] = round(math.sqrt((g["ModelError"] ** 2).mean()), 6)

    # Stability score:  std(ModelError) + max(|ModelError|)
    stability_score = round(
        df["ModelError"].std() + df["ModelError"].abs().max(), 6
    )

    # Path dependence bias:  mean(error_barriers) − mean(error_vanillas)
    path_dep_trades = df[df["ProductType"].isin({"Barrier", "Asian", "ForwardStart"})]
    van_mean  = van_df["ModelError"].mean() if len(van_df) else 0.0
    path_mean = path_dep_trades["ModelError"].mean() if len(path_dep_trades) else 0.0
    path_dep_bias = round(path_mean - van_mean, 6)

    overall_pass = bool(df["Passed"].all())

    validation_summary = {
        "overall_passed":    overall_pass,
        "total_trades":      len(df),
        "n_failed":          int((~df["Passed"]).sum()),
        "stability_score":   stability_score,
        "path_dep_bias":     path_dep_bias,
        "maturity_rmse":     mat_rmse,
        "product_summary":   summary_rows,
        "worst_trades":      df.nlargest(5, "AbsDiff")[
            ["TradeId", "ProductType", "NPV_SABR", "NPV_LV", "ModelError"]
        ].to_dict("records"),
    }

    val_path = output_dir / "validation_summary.json"
    with open(val_path, "w") as fh:
        json.dump(validation_summary, fh, indent=2, default=str)
    logger.info("Validation summary saved → %s", val_path)

    status = "PASSED" if overall_pass else "FAILED"
    logger.info(
        "Validation result: %s  (stability_score=%.4f  path_dep_bias=%.4f)",
        status, stability_score, path_dep_bias,
    )
    return validation_summary
