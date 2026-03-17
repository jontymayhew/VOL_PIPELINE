"""
calibration/sabr.py
-------------------
SABR smile calibration per maturity.

Implementation
--------------
Uses Hagan et al. (2002) approximate SABR formula to compute implied
Black-Scholes volatilities, then fits (alpha, rho, nu) per maturity slice via
scipy least-squares optimisation with beta held fixed from config.

Outputs written to  calibration/sabr/<exp_name>/
  sabr_params.csv         – fitted parameters + RMSE per maturity
  sabr_surface.npy        – implied vol matrix  [n_maturities x n_strikes]
  sabr_surface_meta.json  – strike / maturity labels for the numpy array
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hagan SABR formula
# ---------------------------------------------------------------------------

def sabr_vol(F: float, K: float, T: float,
             alpha: float, beta: float, rho: float, nu: float) -> float:
    """
    Hagan et al. (2002) SABR implied Black (log-normal) volatility.

    Parameters
    ----------
    F     : Forward price.
    K     : Strike.
    T     : Time to expiry in years.
    alpha : SABR alpha (vol-of-vol level).
    beta  : SABR beta (elasticity, 0 = normal, 1 = log-normal).
    rho   : SABR rho (spot/vol correlation).
    nu    : SABR nu (vol-of-vol).

    Returns
    -------
    Implied Black vol (annualised fraction).
    """
    if T <= 0:
        return alpha / (F ** (1.0 - beta))

    if abs(F - K) < 1e-8:  # ATM formula (numerically stable)
        FK_mid = F ** (1.0 - beta)
        term1 = alpha / FK_mid
        term2 = 1.0 + (
            ((1.0 - beta) ** 2 / 24.0) * alpha ** 2 / FK_mid ** 2
            + (rho * beta * nu * alpha) / (4.0 * FK_mid)
            + ((2.0 - 3.0 * rho ** 2) / 24.0) * nu ** 2
        ) * T
        return term1 * term2

    log_FK = math.log(F / K)
    FK_beta = (F * K) ** ((1.0 - beta) / 2.0)
    z = (nu / alpha) * FK_beta * log_FK
    x_z = math.log((math.sqrt(1.0 - 2.0 * rho * z + z ** 2) + z - rho) /
                   (1.0 - rho))

    # Guard against degenerate z
    if abs(z) < 1e-8:
        z_over_xz = 1.0
    else:
        z_over_xz = z / x_z

    numer = alpha * z_over_xz
    denom1 = FK_beta * (
        1.0
        + ((1.0 - beta) ** 2 / 24.0) * log_FK ** 2
        + ((1.0 - beta) ** 4 / 1920.0) * log_FK ** 4
    )
    denom_correction = 1.0 + (
        ((1.0 - beta) ** 2 / 24.0) * alpha ** 2 / ((F * K) ** (1.0 - beta))
        + (rho * beta * nu * alpha) / (4.0 * (F * K) ** ((1.0 - beta) / 2.0))
        + ((2.0 - 3.0 * rho ** 2) / 24.0) * nu ** 2
    ) * T

    return (numer / denom1) * denom_correction


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _residuals(params: np.ndarray, F: float, strikes: list[float],
               market_vols: list[float], T: float, beta: float) -> np.ndarray:
    alpha, rho, nu = params
    model_vols = np.array([sabr_vol(F, K, T, alpha, beta, rho, nu)
                           for K in strikes])
    return model_vols - np.array(market_vols)


def calibrate_sabr_slice(
    forward: float,
    strikes: list[float],
    market_vols: list[float],
    T: float,
    beta: float = 0.5,
    alpha0: Optional[float] = None,
    rho0: float = -0.3,
    nu0: float = 0.4,
    alpha_bounds: tuple = (1e-4, 10.0),
    rho_bounds: tuple = (-0.95, 0.95),
    nu_bounds: tuple = (1e-4, 5.0),
) -> dict:
    """
    Calibrate SABR (alpha, rho, nu) to one maturity slice.

    Returns
    -------
    dict with keys: alpha, beta, rho, nu, rmse, converged
    """
    if alpha0 is None:
        alpha0 = market_vols[len(market_vols) // 2]  # ATM vol as seed

    x0 = [alpha0, rho0, nu0]
    bounds_lo = [alpha_bounds[0], rho_bounds[0], nu_bounds[0]]
    bounds_hi = [alpha_bounds[1], rho_bounds[1], nu_bounds[1]]

    result = least_squares(
        _residuals,
        x0,
        bounds=(bounds_lo, bounds_hi),
        args=(forward, strikes, market_vols, T, beta),
        method="trf",
        max_nfev=2000,
    )

    alpha, rho, nu = result.x
    rmse = float(np.sqrt(np.mean(result.fun ** 2)))

    return {
        "alpha": float(alpha),
        "beta": float(beta),
        "rho": float(rho),
        "nu": float(nu),
        "rmse": rmse,
        "converged": result.success,
    }


def run_sabr_calibration(config: dict, output_dir: Path) -> dict:
    """
    Calibrate SABR per maturity slice and persist results.

    Parameters
    ----------
    config     : Full experiment config dict (from experiment.yaml + grid.yaml).
    output_dir : Path to calibration/sabr/<experiment_name>/ directory.

    Returns
    -------
    dict keyed by maturity label → calibrated params dict
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sabr_cfg = config["sabr"]
    grid_cfg = config["grid"]
    spot = config["spot"]
    rate = config["rate"]
    div_yield = config["div_yield"]
    beta = sabr_cfg["beta"]

    strikes = grid_cfg["strikes"]
    maturities = grid_cfg["maturities"]

    # If market data not available, synthesise from config SABR params
    # (bootstrap: use config params as "truth", add tiny noise to test loop)
    logger.info("Running SABR calibration (beta=%.2f)", beta)

    all_params: dict = {}
    rows = []

    surf_matrix = np.zeros((len(maturities), len(strikes)))

    for mat_idx, mat in enumerate(maturities):
        lbl = mat["label"]
        T = mat["years"]
        F = spot * math.exp((rate - div_yield) * T)

        # --- Market vols:  load from processed/ if available, else synthesise ---
        smile_path = Path(config.get("data_dir", ".")) / "processed" / f"smile_{lbl}.csv"
        if smile_path.exists():
            df = pd.read_csv(smile_path)
            mkt_strikes = df["Strike"].tolist()
            mkt_vols = df["ImpliedVol"].tolist()
        else:
            logger.warning("No market smile data for %s — using config SABR params "
                           "to synthesise training vols", lbl)
            mkt_strikes = [float(k) for k in strikes]
            mkt_vols = [sabr_vol(F, K, T,
                                 sabr_cfg["alpha"], beta,
                                 sabr_cfg["rho"], sabr_cfg["nu"])
                        for K in mkt_strikes]

        params = calibrate_sabr_slice(
            forward=F,
            strikes=mkt_strikes,
            market_vols=mkt_vols,
            T=T,
            beta=beta,
            alpha0=sabr_cfg["alpha"],
            rho0=sabr_cfg["rho"],
            nu0=sabr_cfg["nu"],
            alpha_bounds=tuple(sabr_cfg.get("alpha_bounds", [1e-4, 10.0])),
            rho_bounds=tuple(sabr_cfg.get("rho_bounds", [-0.95, 0.95])),
            nu_bounds=tuple(sabr_cfg.get("nu_bounds", [1e-4, 5.0])),
        )
        params["maturity"] = lbl
        params["T"] = T
        params["forward"] = F
        all_params[lbl] = params
        rows.append(params)

        # Build surface row
        surf_matrix[mat_idx] = [
            sabr_vol(F, float(K), T,
                     params["alpha"], params["beta"],
                     params["rho"], params["nu"])
            for K in strikes
        ]

        flag = "PASS" if params["converged"] else "FAIL"
        logger.info("  %s  alpha=%.4f  rho=%.4f  nu=%.4f  RMSE=%.6f  [%s]",
                    lbl, params["alpha"], params["rho"], params["nu"],
                    params["rmse"], flag)

    # Persist params CSV
    params_df = pd.DataFrame(rows)[
        ["maturity", "T", "forward", "alpha", "beta", "rho", "nu", "rmse", "converged"]
    ]
    params_csv = output_dir / "sabr_params.csv"
    params_df.to_csv(params_csv, index=False)
    logger.info("Saved SABR parameters → %s", params_csv)

    # Persist surface (numpy)
    surf_npy = output_dir / "sabr_surface.npy"
    np.save(surf_npy, surf_matrix)

    meta = {
        "maturities": [m["label"] for m in maturities],
        "strikes": [float(k) for k in strikes],
    }
    with open(output_dir / "sabr_surface_meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)

    logger.info("Saved SABR implied vol surface → %s", surf_npy)
    return all_params
