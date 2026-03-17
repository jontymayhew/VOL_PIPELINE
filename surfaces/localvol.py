"""
surfaces/localvol.py
--------------------
Stage 5 — Dupire local vol construction from a SABR implied-vol surface
using ORE / QuantLib Python bindings.

Steps
-----
1. Sample the SABR surface onto a dense (strike × tenor) grid.
2. Build a BlackVarianceSurface from the dense grid.
3. Wrap with NoExceptLocalVolSurface (Dupire).
4. Sample the local vol onto the same grid and persist.
5. Compute stability metrics (negative variance count, range statistics).

Outputs written to surfaces/localvol/<exp_name>/
  localvol_surface.npy        – local vol matrix  [n_surf_tenors x n_surf_strikes]
  localvol_surface_meta.json  – grid meta
  localvol_stability_metrics.csv
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_localvol_surface(
    config: dict,
    sabr_params: dict,
    output_dir: Path,
):
    """
    Build and persist a Dupire local vol surface derived from calibrated SABR params.

    Parameters
    ----------
    config      : Full experiment config.
    sabr_params : dict[maturity_label → calibrated params dict].
    output_dir  : Output directory (surfaces/localvol/<exp_name>/).

    Returns
    -------
    Tuple: (ORE LocalVolTermStructureHandle, dense_strike_array, dense_tenor_array)
    """
    from ORE import (
        Date, Settings, Actual365Fixed, NullCalendar,
        QuoteHandle, SimpleQuote,
        YieldTermStructureHandle, FlatForward,
        BlackVolTermStructureHandle, BlackVarianceSurface,
        LocalVolTermStructureHandle, NoExceptLocalVolSurface,
        Matrix, Period, Days,
    )

    from calibration.sabr import sabr_vol

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sabr_cfg = config["sabr"]
    lv_cfg = config["localvol"]
    surf_cfg = config["grid"]["surface"]
    maturities = config["grid"]["maturities"]

    # ORE calendar / day count
    dayCount = Actual365Fixed()
    calendar = NullCalendar()

    # Valuation date
    from utils.ore_dates import parse_ore_date
    asofDate = parse_ore_date(config["valuation_date"])
    Settings.instance().evaluationDate = asofDate

    spot = config["spot"]
    rate = config["rate"]
    div_yield = config["div_yield"]

    spotHandle = QuoteHandle(SimpleQuote(spot))
    yieldTS = YieldTermStructureHandle(FlatForward(asofDate, rate, dayCount))
    dividendTS = YieldTermStructureHandle(FlatForward(asofDate, div_yield, dayCount))

    # Dense surface grid
    surf_strikes = np.linspace(surf_cfg["strikes"]["min"],
                               surf_cfg["strikes"]["max"],
                               surf_cfg["strikes"]["n"])
    surf_tenors = np.linspace(surf_cfg["tenors"]["min"],
                              surf_cfg["tenors"]["max"],
                              surf_cfg["tenors"]["n"])

    # Build expiry date list for BlackVarianceSurface
    expiry_dates = []
    for t in surf_tenors:
        days = max(1, int(round(t * 365)))
        expiry_dates.append(asofDate + Period(days, Days))

    # Sample implied vols onto dense grid
    import math
    n_str = len(surf_strikes)
    n_ten = len(surf_tenors)
    vol_matrix = Matrix(n_str, n_ten)

    # We interpolate SABR params linearly in T for tenors between calibrated slices
    slice_years = [m["years"] for m in maturities]
    slice_labels = [m["label"] for m in maturities]

    for j, T in enumerate(surf_tenors):
        # Piecewise-linear interpolation on calibrated SABR params
        if T <= slice_years[0]:
            lbl = slice_labels[0]
        elif T >= slice_years[-1]:
            lbl = slice_labels[-1]
        else:
            lbl = None
            for k in range(len(slice_years) - 1):
                if slice_years[k] <= T <= slice_years[k + 1]:
                    w = (T - slice_years[k]) / (slice_years[k + 1] - slice_years[k])
                    p0 = sabr_params[slice_labels[k]]
                    p1 = sabr_params[slice_labels[k + 1]]
                    interp_params = {
                        "alpha": p0["alpha"] * (1 - w) + p1["alpha"] * w,
                        "beta":  p0["beta"],
                        "rho":   p0["rho"]   * (1 - w) + p1["rho"]   * w,
                        "nu":    p0["nu"]    * (1 - w) + p1["nu"]    * w,
                    }
                    break

        if lbl is not None:
            interp_params = sabr_params[lbl]

        F = spot * math.exp((rate - div_yield) * T)
        for i, K in enumerate(surf_strikes):
            v = sabr_vol(F, K, T,
                         interp_params["alpha"], interp_params["beta"],
                         interp_params["rho"],   interp_params["nu"])
            vol_matrix[i][j] = max(v, 0.001)   # floor to avoid Dupire blow-up

    # Extrapolation mode: constant
    extrap = BlackVarianceSurface.ConstantExtrapolation
    bvs = BlackVarianceSurface(
        asofDate, calendar,
        expiry_dates,
        [float(k) for k in surf_strikes],
        vol_matrix,
        dayCount,
        extrap, extrap,
    )
    bvs.setInterpolation(lv_cfg.get("interpolation", "bicubic"))
    bvs.enableExtrapolation()
    blackVolHandle = BlackVolTermStructureHandle(bvs)

    # Dupire local vol surface
    smoothing = lv_cfg.get("smoothing", 1e-4)
    dupireLV = NoExceptLocalVolSurface(
        blackVolHandle, yieldTS, dividendTS, spotHandle, smoothing)
    dupireLVH = LocalVolTermStructureHandle(dupireLV)

    # Sample to numpy for persistence + diagnostics
    lv_matrix = np.zeros((n_ten, n_str))
    neg_count = 0
    for j, T in enumerate(surf_tenors):
        for i, K in enumerate(surf_strikes):
            try:
                lv = dupireLV.localVol(T, K, True)
                lv_matrix[j, i] = lv
                if lv < 0:
                    neg_count += 1
            except Exception:
                lv_matrix[j, i] = np.nan

    # Persist
    lv_npy = output_dir / "localvol_surface.npy"
    np.save(lv_npy, lv_matrix)

    meta = {
        "tenors": list(surf_tenors),
        "strikes": list(surf_strikes),
    }
    with open(output_dir / "localvol_surface_meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)

    # Stability metrics
    rows = []
    for j, T in enumerate(surf_tenors):
        rows.append({
            "tenor": round(T, 4),
            "lv_min": float(np.nanmin(lv_matrix[j])),
            "lv_max": float(np.nanmax(lv_matrix[j])),
            "lv_mean": float(np.nanmean(lv_matrix[j])),
            "nan_count": int(np.sum(np.isnan(lv_matrix[j]))),
        })
    stab_df = pd.DataFrame(rows)
    stab_csv = output_dir / "localvol_stability_metrics.csv"
    stab_df.to_csv(stab_csv, index=False)

    logger.info(
        "Local vol surface built: shape=%s  neg_count=%d  saved → %s",
        lv_matrix.shape, neg_count, lv_npy,
    )
    return dupireLVH, blackVolHandle, surf_strikes, surf_tenors
