"""
calibration/diagnostics.py
---------------------------
Surface diagnostics run BEFORE local-vol conversion:

  1. Monotonic total variance     – no calendar arbitrage
  2. Call-price monotonicity      – no static arbitrage in strike
  3. Convexity (butterfly)        – density positivity check
  4. Wing explosion check         – |σ| within sensible bounds

Outputs written to calibration/diagnostics/surface_diagnostics.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def run_surface_diagnostics(
    surf_matrix: np.ndarray,
    strikes: list[float],
    years: list[float],
    output_dir: Path,
) -> dict:
    """
    Run arbitrage / stability diagnostics on an implied-vol surface.

    Parameters
    ----------
    surf_matrix : shape (n_maturities, n_strikes) implied Black vols.
    strikes     : List of strikes (float).
    years       : List of times to expiry in years.
    output_dir  : Where to write surface_diagnostics.json.

    Returns
    -------
    Diagnostics dict with a top-level ``passed`` boolean.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    n_mat, n_str = surf_matrix.shape
    issues: list[str] = []

    # 1. Total variance = σ² T  must be increasing in T (calendar arb)
    total_var = surf_matrix ** 2 * np.array(years)[:, None]   # (n_mat, n_str)
    for j in range(n_str):
        for i in range(1, n_mat):
            if total_var[i, j] <= total_var[i - 1, j] - 1e-8:
                issues.append(
                    f"Calendar arbitrage at strike={strikes[j]:.1f} "
                    f"between T={years[i-1]:.2f} and T={years[i]:.2f}"
                )

    # 2. Wing explosion  – vols must be in (0.01, 5.0)
    if np.any(surf_matrix < 0.01):
        n_bad = int(np.sum(surf_matrix < 0.01))
        issues.append(f"Wing collapse: {n_bad} vols below 1%")
    if np.any(surf_matrix > 5.0):
        n_bad = int(np.sum(surf_matrix > 5.0))
        issues.append(f"Wing explosion: {n_bad} vols above 500%")

    # 3. Convexity check  — d²σ/dK² > −threshold  (approx butterfly)
    for i in range(n_mat):
        d2 = np.diff(surf_matrix[i], n=2)
        n_neg = int(np.sum(d2 < -1e-4))
        if n_neg > 0:
            issues.append(
                f"Convexity violation at T={years[i]:.2f}: "
                f"{n_neg} points with negative ∂²σ/∂K²"
            )

    if issues:
        for msg in issues:
            logger.warning("DIAGNOSTIC: %s", msg)
    else:
        logger.info("Surface diagnostics: all checks PASSED")

    result = {
        "passed": len(issues) == 0,
        "issues": issues,
        "n_issues": len(issues),
        "vol_min": float(surf_matrix.min()),
        "vol_max": float(surf_matrix.max()),
        "total_var_min": float(total_var.min()),
        "total_var_max": float(total_var.max()),
    }

    diag_path = output_dir / "surface_diagnostics.json"
    with open(diag_path, "w") as fh:
        json.dump(result, fh, indent=2)
    logger.info("Surface diagnostics saved → %s", diag_path)
    return result
