"""
scripts/runner.py
-----------------
Master pipeline driver.

Usage
-----
    cd ~/libs/ore/quant-research
    python scripts/runner.py                         # full run
    python scripts/runner.py --stage calibration     # one stage only
    python scripts/runner.py --force                 # ignore cache
    python scripts/runner.py --sweep                 # parameter sweep

Pipeline Stages (in order)
--------------------------
  1. market       — snapshot market data (or synthesise from config)
  2. smile        — build smile grid (passes through if using config vols)
  3. calibration  — SABR calibration per maturity
  4. diagnostics  — surface arbitrage / stability checks
  5. localvol     — Dupire local vol conversion
  6. portfolio    — generate ORE portfolio XML
  7. pricing      — run SABR + Local Vol pricing passes
  8. aggregation  — compute comparison metrics
  9. report       — write Markdown validation report
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path when invoked from any directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import yaml

from utils.cache import StageCache, compute_stage_hash
from utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(root: Path, extra: dict | None = None) -> dict:
    """Load and merge experiment.yaml + grid.yaml into one config dict."""
    with open(root / "config" / "experiment.yaml") as fh:
        cfg = yaml.safe_load(fh)
    with open(root / "config" / "grid.yaml") as fh:
        grid = yaml.safe_load(fh)
    cfg["grid"] = grid
    cfg["root_dir"]   = str(root)
    cfg["data_dir"]   = str(root / "data")
    cfg["output_dir"] = str(root / "ore" / "outputs" / cfg["experiment_name"])
    if extra:
        cfg.update(extra)
    return cfg


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def stage_market(cfg: dict, cache: StageCache, force: bool) -> None:
    """Stage 1 — Freeze market data snapshot."""
    stage_hash = compute_stage_hash("market", cfg, [])
    if not force and cache.is_fresh("market", stage_hash):
        logger.info("[CACHED] market stage — skipping")
        return
    logger.info("[RUN] market stage")
    raw_dir = Path(cfg["data_dir"]) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # When live market data is unavailable the pipeline synthesises from config.
    # Write a metadata sentinel so downstream stages can detect the data source.
    import json
    meta = {
        "source":          "synthetic",
        "valuation_date":  cfg["valuation_date"],
        "underlying":      cfg["underlying"],
        "spot":            cfg["spot"],
        "rate":            cfg["rate"],
        "div_yield":       cfg["div_yield"],
    }
    with open(raw_dir / "market_meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)
    logger.info("Market metadata written → %s", raw_dir / "market_meta.json")
    cache.record("market", stage_hash)


def stage_smile(cfg: dict, cache: StageCache, force: bool) -> None:
    """Stage 2 — Build processed smile grid."""
    input_files = [
        Path(cfg["data_dir"]) / "raw" / "market_meta.json",
    ]
    stage_hash = compute_stage_hash("smile", cfg, input_files)
    if not force and cache.is_fresh("smile", stage_hash):
        logger.info("[CACHED] smile stage — skipping")
        return
    logger.info("[RUN] smile stage")
    proc_dir = Path(cfg["data_dir"]) / "processed"
    proc_dir.mkdir(parents=True, exist_ok=True)
    # Smile grid is derived analytically from SABR config — no raw file needed.
    logger.info("Smile grid: using SABR config params (no external market data)")
    import json
    grid_meta = {
        "maturities": [m["label"] for m in cfg["grid"]["maturities"]],
        "strikes":    cfg["grid"]["strikes"],
        "source":     "sabr_config",
    }
    with open(proc_dir / "smile_grid_meta.json", "w") as fh:
        json.dump(grid_meta, fh, indent=2)
    cache.record("smile", stage_hash)


def stage_calibration(cfg: dict, cache: StageCache, force: bool) -> dict:
    """Stage 3 — SABR calibration per maturity slice."""
    input_files = [
        Path(cfg["data_dir"]) / "processed" / "smile_grid_meta.json",
    ]
    stage_hash = compute_stage_hash("calibration", cfg, input_files)
    if not force and cache.is_fresh("calibration", stage_hash):
        logger.info("[CACHED] calibration stage — loading from disk")
        return _load_sabr_params(cfg)

    logger.info("[RUN] calibration stage")
    from calibration.sabr import run_sabr_calibration
    out = Path(cfg["root_dir"]) / "calibration" / "sabr" / cfg["experiment_name"]
    sabr_params = run_sabr_calibration(cfg, out)
    cache.record("calibration", stage_hash)
    return sabr_params


def stage_diagnostics(cfg: dict, surf_matrix, cache: StageCache, force: bool) -> dict:
    """Stage 4 — Surface diagnostics."""
    import numpy as np
    import hashlib
    surf_hash = hashlib.sha256(surf_matrix.tobytes()).hexdigest()
    stage_hash = compute_stage_hash("diagnostics", cfg, []) + surf_hash
    if not force and cache.is_fresh("diagnostics", stage_hash):
        logger.info("[CACHED] diagnostics stage — skipping")
        import json
        diag_path = (Path(cfg["root_dir"]) / "calibration" / "diagnostics"
                     / "surface_diagnostics.json")
        if diag_path.exists():
            with open(diag_path) as fh:
                return json.load(fh)

    logger.info("[RUN] diagnostics stage")
    from calibration.diagnostics import run_surface_diagnostics
    out = Path(cfg["root_dir"]) / "calibration" / "diagnostics"
    years = [m["years"] for m in cfg["grid"]["maturities"]]
    strikes = [float(k) for k in cfg["grid"]["strikes"]]
    diagnostics = run_surface_diagnostics(surf_matrix, strikes, years, out)
    cache.record("diagnostics", stage_hash)
    return diagnostics


def stage_localvol(cfg: dict, sabr_params: dict, cache: StageCache, force: bool):
    """Stage 5 — Dupire local vol conversion."""
    cal_csv = (Path(cfg["root_dir"]) / "calibration" / "sabr"
               / cfg["experiment_name"] / "sabr_params.csv")
    stage_hash = compute_stage_hash("localvol", cfg, [cal_csv])
    if not force and cache.is_fresh("localvol", stage_hash):
        logger.info("[CACHED] localvol stage — skipping (rebuilding handles from disk)")
        # Rebuild ORE handles from persisted surface
        return _rebuild_lv_handles(cfg, sabr_params)

    logger.info("[RUN] localvol stage")
    from surfaces.localvol import build_localvol_surface
    out = Path(cfg["root_dir"]) / "surfaces" / "localvol" / cfg["experiment_name"]
    lv_handle, bv_handle, surf_str, surf_ten = build_localvol_surface(
        cfg, sabr_params, out)
    cache.record("localvol", stage_hash)
    return lv_handle, bv_handle, surf_str, surf_ten


def stage_portfolio(cfg: dict, cache: StageCache, force: bool) -> Path:
    """Stage 6 — Generate portfolio XML."""
    stage_hash = compute_stage_hash("portfolio", cfg, [])
    if not force and cache.is_fresh("portfolio", stage_hash):
        out_path = (Path(cfg["root_dir"]) / "ore" / "portfolio"
                    / cfg["experiment_name"] / "portfolio_validation.xml")
        if out_path.exists():
            logger.info("[CACHED] portfolio stage — skipping")
            return out_path

    logger.info("[RUN] portfolio stage")
    from ore.portfolio_gen import generate_portfolio
    out = (Path(cfg["root_dir"]) / "ore" / "portfolio" / cfg["experiment_name"])
    port_path = generate_portfolio(cfg, out)
    cache.record("portfolio", stage_hash)
    return port_path


def stage_pricing(cfg: dict, sabr_params: dict, bv_handle,
                  cache: StageCache, force: bool) -> tuple:
    """Stage 7 — Run SABR + Local Vol pricing."""
    port_path = (Path(cfg["root_dir"]) / "ore" / "portfolio"
                 / cfg["experiment_name"] / "portfolio_validation.xml")
    stage_hash = compute_stage_hash("pricing", cfg, [port_path])
    out = Path(cfg["output_dir"])

    if not force and cache.is_fresh("pricing", stage_hash):
        sabr_csv = out / "npv_sabr.csv"
        lv_csv   = out / "npv_localvol.csv"
        if sabr_csv.exists() and lv_csv.exists():
            logger.info("[CACHED] pricing stage — skipping")
            import pandas as pd
            return pd.read_csv(sabr_csv), pd.read_csv(lv_csv)

    logger.info("[RUN] pricing stage — SABR pass")
    from pricing.ore_runner import price_sabr_model, price_localvol_model
    sabr_df = price_sabr_model(cfg, sabr_params, out)
    logger.info("[RUN] pricing stage — Local Vol pass")
    lv_df   = price_localvol_model(cfg, bv_handle, out)
    cache.record("pricing", stage_hash)
    return sabr_df, lv_df


def stage_aggregation(cfg: dict, cache: StageCache, force: bool) -> dict:
    """Stage 8 — Compute validation metrics."""
    out = Path(cfg["output_dir"])
    sabr_csv = out / "npv_sabr.csv"
    lv_csv   = out / "npv_localvol.csv"
    stage_hash = compute_stage_hash("aggregation", cfg, [sabr_csv, lv_csv])
    agg_out = Path(cfg["root_dir"]) / "aggregation" / cfg["experiment_name"]

    if not force and cache.is_fresh("aggregation", stage_hash):
        val_path = agg_out / "validation_summary.json"
        if val_path.exists():
            logger.info("[CACHED] aggregation stage — skipping")
            import json
            with open(val_path) as fh:
                return json.load(fh)

    logger.info("[RUN] aggregation stage")
    from aggregation.compare import compare_results
    summary = compare_results(sabr_csv, lv_csv, agg_out)
    cache.record("aggregation", stage_hash)
    return summary


def stage_report(cfg: dict, sabr_params: dict, surface_diagnostics: dict,
                 validation_summary: dict, cache: StageCache, force: bool) -> Path:
    """Stage 9 — Write Markdown validation report."""
    agg_out      = Path(cfg["root_dir"]) / "aggregation" / cfg["experiment_name"]
    comparison_csv = agg_out / "comparison_metrics.csv"
    stage_hash = compute_stage_hash("report", cfg, [comparison_csv])

    if not force and cache.is_fresh("report", stage_hash):
        logger.info("[CACHED] report stage — skipping")
        # return most recent report
        reports = sorted(
            (Path(cfg["root_dir"]) / "reports").glob("validation_*.md"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if reports:
            return reports[0]

    logger.info("[RUN] report stage")
    from aggregation.report import generate_report
    report_dir = Path(cfg["root_dir"]) / "reports"
    report_path = generate_report(
        cfg, sabr_params, surface_diagnostics, validation_summary,
        comparison_csv, report_dir,
    )
    cache.record("report", stage_hash)
    return report_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_sabr_params(cfg: dict) -> dict:
    import pandas as pd
    params_csv = (Path(cfg["root_dir"]) / "calibration" / "sabr"
                  / cfg["experiment_name"] / "sabr_params.csv")
    df = pd.read_csv(params_csv)
    return {
        row["maturity"]: {
            "alpha":     row["alpha"],
            "beta":      row["beta"],
            "rho":       row["rho"],
            "nu":        row["nu"],
            "rmse":      row["rmse"],
            "converged": bool(row["converged"]),
        }
        for _, row in df.iterrows()
    }


def _rebuild_lv_handles(cfg: dict, sabr_params: dict):
    """Re-build ORE handles from persisted SABR params (cache hit bypass)."""
    from surfaces.localvol import build_localvol_surface
    out = Path(cfg["root_dir"]) / "surfaces" / "localvol" / cfg["experiment_name"]
    return build_localvol_surface(cfg, sabr_params, out)


def _load_sabr_surface(cfg: dict) -> "np.ndarray":
    import numpy as np
    surf_npy = (Path(cfg["root_dir"]) / "calibration" / "sabr"
                / cfg["experiment_name"] / "sabr_surface.npy")
    return np.load(surf_npy)


# ---------------------------------------------------------------------------
# Parameter sweep
# ---------------------------------------------------------------------------

def run_sweep(cfg: dict, root: Path) -> None:
    """Run the full pipeline across a grid of (beta × strike_nodes × smoothing)."""
    sweep_cfg = cfg.get("parameter_sweep", {})
    betas        = sweep_cfg.get("beta",         [cfg["sabr"]["beta"]])
    node_counts  = sweep_cfg.get("strike_nodes", [cfg["grid"]["strike_nodes"]])
    smoothings   = sweep_cfg.get("smoothing",    [cfg["localvol"]["smoothing"]])

    total = len(betas) * len(node_counts) * len(smoothings)
    logger.info("Parameter sweep: %d combinations", total)
    idx = 0
    for beta in betas:
        for nodes in node_counts:
            for smooth in smoothings:
                idx += 1
                from copy import deepcopy
                run_cfg = deepcopy(cfg)
                run_cfg["sabr"]["beta"]           = beta
                run_cfg["grid"]["strike_nodes"]   = nodes
                run_cfg["localvol"]["smoothing"]  = smooth
                exp_tag = f"beta{beta}_nodes{nodes}_smooth{smooth:.0e}"
                run_cfg["experiment_name"] = (
                    f"{cfg['experiment_name']}_{exp_tag}"
                )
                run_cfg["output_dir"] = str(
                    root / "ore" / "outputs" / run_cfg["experiment_name"]
                )
                logger.info("Sweep run %d/%d: %s", idx, total, exp_tag)
                cache = StageCache(root / "data" / "cache" / exp_tag)
                run_pipeline(run_cfg, root, cache, force=False)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline(cfg: dict, root: Path, cache: StageCache, force: bool) -> None:
    """Execute all pipeline stages in order."""
    t_start = time.perf_counter()
    logger.info("=" * 60)
    logger.info("Pipeline: %s", cfg["experiment_name"])
    logger.info("=" * 60)

    import numpy as np

    stage_market(cfg, cache, force)
    stage_smile(cfg, cache, force)
    sabr_params = stage_calibration(cfg, cache, force)

    # Load SABR surface for diagnostics
    surf_path = (root / "calibration" / "sabr"
                 / cfg["experiment_name"] / "sabr_surface.npy")
    if surf_path.exists():
        surf_matrix = np.load(surf_path)
    else:
        # Rebuild inline
        from calibration.sabr import sabr_vol
        import math
        mats = cfg["grid"]["maturities"]
        strikes = cfg["grid"]["strikes"]
        spot = cfg["spot"]; rate = cfg["rate"]; div = cfg["div_yield"]
        surf_matrix = np.zeros((len(mats), len(strikes)))
        for i, mat in enumerate(mats):
            T = mat["years"]
            F = spot * math.exp((rate - div) * T)
            p = sabr_params[mat["label"]]
            for j, K in enumerate(strikes):
                surf_matrix[i, j] = sabr_vol(
                    F, float(K), T, p["alpha"], p["beta"], p["rho"], p["nu"])

    diagnostics = stage_diagnostics(cfg, surf_matrix, cache, force)

    lv_result = stage_localvol(cfg, sabr_params, cache, force)
    _lv_handle, bv_handle, _surf_str, _surf_ten = lv_result

    stage_portfolio(cfg, cache, force)
    _sabr_df, _lv_df = stage_pricing(cfg, sabr_params, bv_handle, cache, force)
    validation_summary = stage_aggregation(cfg, cache, force)
    report_path = stage_report(cfg, sabr_params, diagnostics,
                               validation_summary, cache, force)

    elapsed = time.perf_counter() - t_start
    status  = "PASSED" if validation_summary["overall_passed"] else "FAILED"
    logger.info("Pipeline complete in %.1f s — %s", elapsed, status)
    logger.info("Report → %s", report_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="SABR ↔ Local Vol validation pipeline"
    )
    parser.add_argument(
        "--root", default=str(_ROOT),
        help="Project root directory (default: parent of this script)",
    )
    parser.add_argument(
        "--stage", default=None,
        choices=["market", "smile", "calibration", "diagnostics",
                 "localvol", "portfolio", "pricing", "aggregation", "report"],
        help="Run a single stage only (default: run all)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore cache and re-run all stages",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run parameter sweep defined in experiment.yaml",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    cfg  = load_config(root)
    setup_logging(root / "logs")

    cache = StageCache(root / "data" / "cache")

    if args.sweep:
        run_sweep(cfg, root)
        return 0

    if args.stage:
        # Single-stage execution (convenience mode, mostly for development)
        logger.info("Running single stage: %s", args.stage)
        if args.stage == "market":
            stage_market(cfg, cache, force=True)
        elif args.stage == "smile":
            stage_smile(cfg, cache, force=True)
        elif args.stage == "calibration":
            stage_calibration(cfg, cache, force=True)
        elif args.stage == "portfolio":
            stage_portfolio(cfg, cache, force=True)
        elif args.stage == "report":
            sabr_params = _load_sabr_params(cfg)
            import json, numpy as np
            val_path = (root / "aggregation" / cfg["experiment_name"]
                        / "validation_summary.json")
            diag_path = root / "calibration" / "diagnostics" / "surface_diagnostics.json"
            with open(val_path)  as fh: vs = json.load(fh)
            with open(diag_path) as fh: diag = json.load(fh)
            stage_report(cfg, sabr_params, diag, vs, cache, force=True)
        else:
            logger.warning("Stage '%s' requires prior stages; running full pipeline",
                           args.stage)
            run_pipeline(cfg, root, cache, force=args.force)
        return 0

    run_pipeline(cfg, root, cache, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
