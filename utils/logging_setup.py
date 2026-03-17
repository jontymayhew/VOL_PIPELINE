"""
utils/logging_setup.py
-----------------------
Centralised logging configuration for the quant-research pipeline.
"""

import logging
import sys
from pathlib import Path


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """
    Configure root logger to write to both stdout and a rotating log file.

    Parameters
    ----------
    log_dir : Directory where the log file is written.
    level   : Logging level (default INFO).

    Returns
    -------
    Root logger instance.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    ch.setLevel(level)

    # File handler
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(level)

    if not root.handlers:
        root.addHandler(ch)
        root.addHandler(fh)

    return root
