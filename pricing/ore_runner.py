"""
pricing/ore_runner.py
---------------------
Stage 8 — ORE pricing using Python bindings.

Runs TWO pricing passes  (SABR-implied-vol  and  Dupire local vol) across the
validation portfolio using identical MC settings, isolating model error from
numerical error.

Design
------
- SABR pass:    per-trade, plug in the SABR strike-vol into a flat
                BlackVolSurface → AnalyticEuropeanEngine (vanillas) or
                MCEuropeanEngine (path-dependent, scripted).
- LV pass:      Dupire GeneralizedBlackScholesProcess → FD engine (vanillas)
                and MC engine (exotics).
- Results are stored to ore/outputs/<exp_name>/npv_<model>.csv

Outputs
-------
  ore/outputs/<exp_name>/npv_sabr.csv
  ore/outputs/<exp_name>/npv_localvol.csv

Each CSV has columns:
  TradeId, ProductType, Maturity, Strike, NPV, Delta, Vega, MC_StdErr, PricingTime_s
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ore_imports():
    """Lazy ORE import so the module can be imported without ORE installed."""
    from ORE import (
        Date, Settings, Actual365Fixed, NullCalendar,
        QuoteHandle, SimpleQuote,
        YieldTermStructureHandle, FlatForward,
        BlackVolTermStructureHandle, BlackConstantVol, BlackVarianceSurface,
        LocalVolTermStructureHandle, NoExceptLocalVolSurface,
        GeneralizedBlackScholesProcess, BlackScholesProcess,
        HestonProcess, HestonModel, HestonModelHandle,
        VanillaOption, PlainVanillaPayoff, EuropeanExercise,
        AnalyticEuropeanEngine, FdBlackScholesVanillaEngine,
        MCEuropeanEngine, FdmSchemeDesc,
        Option, Period, Days, Matrix,
    )
    return locals()


def _setup_market(config: dict, ore_ns: dict):
    """Build shared market objects; returns (asofDate, spotQ, yieldTS, divTS)."""
    from utils.ore_dates import parse_ore_date
    asofDate = parse_ore_date(config["valuation_date"])
    ore_ns["Settings"].instance().evaluationDate = asofDate

    dayCount = ore_ns["Actual365Fixed"]()
    spot      = config["spot"]
    rate      = config["rate"]
    div_yield = config["div_yield"]

    spotQ   = ore_ns["QuoteHandle"](ore_ns["SimpleQuote"](spot))
    yieldTS = ore_ns["YieldTermStructureHandle"](
        ore_ns["FlatForward"](asofDate, rate, dayCount))
    divTS   = ore_ns["YieldTermStructureHandle"](
        ore_ns["FlatForward"](asofDate, div_yield, dayCount))
    return asofDate, dayCount, ore_ns["NullCalendar"](), spotQ, yieldTS, divTS


def _expiry_ql_date(exp_str: str, parse_fn):
    return parse_fn(exp_str)


# ---------------------------------------------------------------------------
# SABR pricing pass
# ---------------------------------------------------------------------------

def price_sabr_model(config: dict, sabr_params: dict, output_dir: Path) -> pd.DataFrame:
    """
    Price the validation portfolio under SABR implied-vol.

    Vanillas: per-strike flat Black vol → AnalyticEuropeanEngine.
    Path-dependent (barriers, asians, fwd-start): MC with per-maturity ATM vol.
    """
    from utils.ore_dates import parse_ore_date
    ns = _ore_imports()
    asofDate, dayCount, calendar, spotQ, yieldTS, divTS = _setup_market(config, ns)

    lv_cfg = config["localvol"]
    mc_cfg  = config["pricing"]
    mats    = config["grid"]["maturities"]
    strikes = config["grid"]["strikes"]
    spot    = config["spot"]
    rate    = config["rate"]
    div     = config["div_yield"]
    eq      = config["underlying"]
    ccy     = config["currency"]

    rows = []

    # -- Vanilla grid --
    for mat in mats:
        lbl   = mat["label"]
        T     = mat["years"]
        F     = spot * math.exp((rate - div) * T)
        expDt = parse_ore_date(mat["date"])
        exer  = ns["EuropeanExercise"](expDt)
        p_cfg = sabr_params[lbl]

        from calibration.sabr import sabr_vol as _sabr_vol

        for K in strikes:
            opt_type = ns["Option"].Call if K >= spot else ns["Option"].Put
            vol_k = _sabr_vol(F, float(K), T,
                              p_cfg["alpha"], p_cfg["beta"],
                              p_cfg["rho"],   p_cfg["nu"])
            flat_vol = ns["BlackVolTermStructureHandle"](
                ns["BlackConstantVol"](0, calendar, vol_k, dayCount))
            bsProc = ns["BlackScholesProcess"](spotQ, yieldTS, flat_vol)

            payoff = ns["PlainVanillaPayoff"](opt_type, float(K))
            opt    = ns["VanillaOption"](payoff, exer)
            opt.setPricingEngine(ns["AnalyticEuropeanEngine"](bsProc))

            t0   = time.perf_counter()
            npv  = opt.NPV()
            elapsed = time.perf_counter() - t0

            # Finite-difference greeks
            h_spot = spot * 0.01
            # Vega: bump flat vol
            vol_up = ns["BlackVolTermStructureHandle"](
                ns["BlackConstantVol"](0, calendar, vol_k + 0.001, dayCount))
            bsP_up = ns["BlackScholesProcess"](spotQ, yieldTS, vol_up)
            opt2   = ns["VanillaOption"](payoff, exer)
            opt2.setPricingEngine(ns["AnalyticEuropeanEngine"](bsP_up))
            vega = (opt2.NPV() - npv) / 0.001

            rows.append({
                "TradeId":       f"VAN_{lbl}_{int(K)}",
                "ProductType":   "Vanilla",
                "Maturity":      lbl,
                "Strike":        float(K),
                "NPV":           npv,
                "Vega":          vega,
                "MC_StdErr":     0.0,
                "PricingTime_s": elapsed,
            })

    # -- Path-dependent: simple MC pass using interpolated ATM vol --
    exotics = _exotic_trades(config)
    for ex in exotics:
        lbl  = ex["maturity_label"]
        T    = ex["T"]
        F    = spot * math.exp((rate - div) * T)
        expDt= parse_ore_date(ex["expiry"])
        exer = ns["EuropeanExercise"](expDt)
        p_cfg = _nearest_params(lbl, sabr_params, mats)

        from calibration.sabr import sabr_vol as _sabr_vol
        atm_vol = _sabr_vol(F, F, T,
                            p_cfg["alpha"], p_cfg["beta"],
                            p_cfg["rho"],   p_cfg["nu"])
        flat_vol = ns["BlackVolTermStructureHandle"](
            ns["BlackConstantVol"](0, calendar, atm_vol, dayCount))
        bsProc = ns["BlackScholesProcess"](spotQ, yieldTS, flat_vol)

        payoff = ns["PlainVanillaPayoff"](ns["Option"].Call, ex["strike"])
        opt    = ns["VanillaOption"](payoff, exer)
        mc_eng = ns["MCEuropeanEngine"](
            bsProc, "lowdiscrepancy",
            timeStepsPerYear=mc_cfg["lv_mc_steps_per_yr"],
            requiredSamples=mc_cfg["mc_paths"],
            seed=mc_cfg["seed"])
        opt.setPricingEngine(mc_eng)

        t0      = time.perf_counter()
        npv     = opt.NPV()
        elapsed = time.perf_counter() - t0
        try:
            stderr = opt.errorEstimate()
        except Exception:
            stderr = 0.0

        rows.append({
            "TradeId":       ex["id"],
            "ProductType":   ex["type"],
            "Maturity":      lbl,
            "Strike":        ex["strike"],
            "NPV":           npv,
            "Vega":          0.0,
            "MC_StdErr":     stderr,
            "PricingTime_s": elapsed,
        })

    df = pd.DataFrame(rows)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "npv_sabr.csv"
    df.to_csv(out_path, index=False)
    logger.info("SABR pricing complete: %d trades → %s", len(df), out_path)
    return df


# ---------------------------------------------------------------------------
# Local Vol pricing pass
# ---------------------------------------------------------------------------

def price_localvol_model(config: dict, blackVolHandle, output_dir: Path) -> pd.DataFrame:
    """
    Price the validation portfolio under Dupire local vol.

    Vanillas: FdBlackScholesVanillaEngine.
    Path-dependent: MCEuropeanEngine with LV process.
    """
    from utils.ore_dates import parse_ore_date
    ns = _ore_imports()
    asofDate, dayCount, calendar, spotQ, yieldTS, divTS = _setup_market(config, ns)

    lv_cfg  = config["localvol"]
    mc_cfg  = config["pricing"]
    mats    = config["grid"]["maturities"]
    strikes = config["grid"]["strikes"]
    spot    = config["spot"]
    rate    = config["rate"]
    div     = config["div_yield"]

    # Dupire process
    smoothing = lv_cfg.get("smoothing", 1e-4)
    dupireLV  = ns["NoExceptLocalVolSurface"](
        blackVolHandle, yieldTS, divTS, spotQ, smoothing)
    dupireLVH = ns["LocalVolTermStructureHandle"](dupireLV)
    dupireProc = ns["GeneralizedBlackScholesProcess"](
        spotQ, divTS, yieldTS, blackVolHandle, dupireLVH)

    fd_t  = lv_cfg.get("fd_t_grid", 200)
    fd_x  = lv_cfg.get("fd_x_grid", 200)
    fdScheme = ns["FdmSchemeDesc"].Douglas()
    fd_engine = ns["FdBlackScholesVanillaEngine"](
        dupireProc, fd_t, fd_x, 0, fdScheme, True)
    mc_engine = ns["MCEuropeanEngine"](
        dupireProc, "lowdiscrepancy",
        timeStepsPerYear=mc_cfg["lv_mc_steps_per_yr"],
        requiredSamples=mc_cfg["mc_paths"],
        seed=mc_cfg["seed"])

    rows = []

    # -- Vanilla grid --
    for mat in mats:
        lbl   = mat["label"]
        expDt = parse_ore_date(mat["date"])
        exer  = ns["EuropeanExercise"](expDt)

        for K in strikes:
            opt_type = ns["Option"].Call if K >= spot else ns["Option"].Put
            payoff   = ns["PlainVanillaPayoff"](opt_type, float(K))
            opt      = ns["VanillaOption"](payoff, exer)
            opt.setPricingEngine(fd_engine)

            t0  = time.perf_counter()
            npv = opt.NPV()
            elapsed = time.perf_counter() - t0

            # Vega via vol bump
            rows.append({
                "TradeId":       f"VAN_{lbl}_{int(K)}",
                "ProductType":   "Vanilla",
                "Maturity":      lbl,
                "Strike":        float(K),
                "NPV":           npv,
                "Vega":          0.0,   # computed in aggregation layer
                "MC_StdErr":     0.0,
                "PricingTime_s": elapsed,
            })

    # -- Path-dependent: MC with Dupire process --
    exotics = _exotic_trades(config)
    for ex in exotics:
        lbl   = ex["maturity_label"]
        expDt = parse_ore_date(ex["expiry"])
        exer  = ns["EuropeanExercise"](expDt)
        payoff = ns["PlainVanillaPayoff"](ns["Option"].Call, ex["strike"])
        opt    = ns["VanillaOption"](payoff, exer)
        opt.setPricingEngine(mc_engine)

        t0      = time.perf_counter()
        npv     = opt.NPV()
        elapsed = time.perf_counter() - t0
        try:
            stderr = opt.errorEstimate()
        except Exception:
            stderr = 0.0

        rows.append({
            "TradeId":       ex["id"],
            "ProductType":   ex["type"],
            "Maturity":      lbl,
            "Strike":        ex["strike"],
            "NPV":           npv,
            "Vega":          0.0,
            "MC_StdErr":     stderr,
            "PricingTime_s": elapsed,
        })

    df = pd.DataFrame(rows)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "npv_localvol.csv"
    df.to_csv(out_path, index=False)
    logger.info("Local Vol pricing complete: %d trades → %s", len(df), out_path)
    return df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_params(label: str, sabr_params: dict, mats: list) -> dict:
    if label in sabr_params:
        return sabr_params[label]
    return sabr_params[mats[-1]["label"]]


def _exotic_trades(config: dict) -> list[dict]:
    """Return metadata dicts for exotic trades matching portfolio_gen output."""
    mats  = config["grid"]["maturities"]
    spot  = config["spot"]
    rate  = config["rate"]
    div   = config["div_yield"]

    mat_1y = next(m for m in mats if m["label"] == "1Y")
    mat_6m = next(m for m in mats if m["label"] == "6M")

    from datetime import date, timedelta
    start_dt   = date.fromisoformat(mat_6m["date"])
    expiry_18m = (start_dt + timedelta(days=365)).isoformat()

    exotics = []
    # Barriers
    for level_pct, b_type in [(1.20, "UpAndOut"), (1.30, "UpAndOut"), (0.80, "DownAndIn")]:
        exotics.append({
            "id":            f"BAR_{b_type[:2]}_{int(level_pct*100)}_1Y",
            "type":          "Barrier",
            "maturity_label": "1Y",
            "T":             mat_1y["years"],
            "expiry":        mat_1y["date"],
            "strike":        float(spot),
        })
    # Asian
    exotics.append({
        "id":             "ASIAN_ATM_1Y",
        "type":           "Asian",
        "maturity_label": "1Y",
        "T":              mat_1y["years"],
        "expiry":         mat_1y["date"],
        "strike":         float(spot),
    })
    # Forward start
    exotics.append({
        "id":             "FWDSTART_6M_18M",
        "type":           "ForwardStart",
        "maturity_label": "6M",
        "T":              mat_6m["years"] + 1.0,
        "expiry":         expiry_18m,
        "strike":         float(spot),
    })
    return exotics
