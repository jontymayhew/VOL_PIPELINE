# SABR ↔ Local Vol Consistency Validation

## Overview

This is a **quantitative pipeline** that validates the consistency between two widely-used option pricing models:

- **SABR** (Stochastic Alpha Beta Rho) — a stochastic volatility model by Hagan et al. (2002)
- **Dupire Local Vol** — a deterministic local volatility model derived from the same implied vol surface

The goal is to confirm that, when the local vol surface is built from SABR-implied vols, both models price the same portfolio within acceptable tolerances. This is used to validate calibration quality and model implementation correctness.

---

## Prerequisites

- Python 3.9+
- [ORE (Open Source Risk Engine)](https://www.opensourcerisk.org/) Python bindings — `import ORE`
- `numpy`, `pandas`, `scipy`, `pyyaml`

---

## Project Structure

```
Vol_Pipeline/
├── config/
│   ├── experiment.yaml     # Market params, SABR params, MC settings
│   └── grid.yaml           # Strike grid, maturity dates, surface resolution
├── scripts/
│   └── runner.py           # Master pipeline driver — start here
├── calibration/
│   ├── sabr.py             # Hagan SABR formula + calibration (scipy least-squares)
│   └── diagnostics.py      # Arbitrage / stability checks on the vol surface
├── surfaces/
│   └── localvol.py         # Dupire local vol construction via ORE bindings
├── ore/
│   ├── portfolio_gen.py    # Generates the ORE trade portfolio XML
│   └── portfolio/          # Output: portfolio_validation.xml
├── pricing/
│   └── ore_runner.py       # Prices the portfolio under SABR and Local Vol
├── aggregation/
│   ├── compare.py          # Merges results, computes vega-normalised model error
│   └── report.py           # Writes the Markdown validation report
├── data/
│   ├── raw/                # market_meta.json (synthetic data sentinel)
│   ├── processed/          # smile_grid_meta.json
│   └── cache/              # Stage hashes for incremental re-runs
└── reports/
    └── validation_*.md     # Final output report
```

---

## Configuration

All parameters live in two YAML files — no code changes needed for typical runs.

**`config/experiment.yaml`**
```yaml
spot: 100.0          # Synthetic spot price
rate: 0.02           # Flat risk-free rate
div_yield: 0.0       # Dividend yield
sabr:
  alpha: 2.00        # Vol-of-vol level
  beta:  0.50        # Elasticity (0=normal, 1=lognormal)
  rho:  -0.30        # Spot/vol correlation
  nu:    0.40        # Vol-of-vol
```

**`config/grid.yaml`**
```yaml
strikes: [60, 70, 80, 90, 100, 110, 120, 130, 140]
maturities:
  - label: "6M"   date: "2026-09-15"
  - label: "1Y"   date: "2027-03-15"
  - label: "2Y"   date: "2028-03-15"
```

> **Note:** Market data is entirely synthetic — derived analytically from the SABR config parameters. No external data source is used.

---

## Pipeline Stages

The pipeline runs 9 stages in sequence:

| # | Stage | What it does |
|---|-------|-------------|
| 1 | `market` | Writes a synthetic market metadata sentinel to `data/raw/market_meta.json` |
| 2 | `smile` | Builds the smile grid metadata from SABR config params |
| 3 | `calibration` | Fits SABR `(alpha, rho, nu)` per maturity via scipy least-squares; saves `sabr_params.csv` and `sabr_surface.npy` |
| 4 | `diagnostics` | Checks the SABR surface for calendar spread / butterfly arbitrage violations |
| 5 | `localvol` | Builds a dense `BlackVarianceSurface` from SABR vols, wraps with Dupire `NoExceptLocalVolSurface` via ORE |
| 6 | `portfolio` | Generates `portfolio_validation.xml` — vanilla options, barriers, Asian options, forward-start options |
| 7 | `pricing` | Prices the portfolio twice: SABR (analytic/MC) and Local Vol (FD/MC) using ORE Python bindings |
| 8 | `aggregation` | Computes vega-normalised model error per trade; applies pass/fail thresholds (10% vanilla, 20% exotics) |
| 9 | `report` | Writes `reports/validation_<date>.md` with full results |

---

## Running the Pipeline

```bash
cd ~/libs/ore/Vol_Pipeline

# Full run (uses cache to skip unchanged stages)
python scripts/runner.py

# Ignore cache and re-run everything
python scripts/runner.py --force

# Run a single stage only (useful during development)
python scripts/runner.py --stage calibration
python scripts/runner.py --stage pricing

# Parameter sweep over beta × strike_nodes × smoothing
python scripts/runner.py --sweep
```

---

## Caching

Each stage computes a hash over its inputs and config. If the hash hasn't changed since the last run, the stage is skipped. Use `--force` to override. Cache state is stored in `data/cache/cache_manifest.json`.

---

## Outputs

| File | Description |
|------|-------------|
| `calibration/sabr/.../sabr_params.csv` | Fitted SABR parameters + RMSE per maturity |
| `surfaces/localvol/.../localvol_stability_metrics.csv` | Local vol surface quality stats |
| `ore/portfolio/.../portfolio_validation.xml` | Trade book (4 product types) |
| `ore/outputs/.../npv_sabr.csv` | SABR model prices |
| `ore/outputs/.../npv_localvol.csv` | Local Vol model prices |
| `aggregation/.../comparison_metrics.csv` | Per-trade model error |
| `aggregation/.../validation_summary.json` | Overall PASS/FAIL verdict |
| `reports/validation_<date>.md` | Full Markdown report |

---

## Parameter Sweep

Defined in `experiment.yaml` under `parameter_sweep`:
```yaml
parameter_sweep:
  beta: [0.3, 0.5, 0.7]
  strike_nodes: [15, 25, 40]
  smoothing: [1.0e-4, 1.0e-3]
```
Runs 3 × 3 × 2 = **18 pipeline combinations**, each with its own output directory and cache.

---

## Validation Thresholds

| Product | Threshold |
|---------|-----------|
| Vanilla options | `\|model error\| < 10%` (vega-normalised) |
| Barriers, Asians, Forward-starts | `\|model error\| < 20%` |
