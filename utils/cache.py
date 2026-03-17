"""
utils/cache.py
--------------
SHA-256 based stage caching.  Each pipeline stage hashes its config + input
files and skips execution when a matching output fingerprint already exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MANIFEST = "cache_manifest.json"


def _hash_value(obj: Any) -> str:
    """Return a stable hex digest for a JSON-serialisable value."""
    raw = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_stage_hash(stage_name: str, config: dict, input_paths: list[Path]) -> str:
    """
    Compute a fingerprint for a pipeline stage.

    Parameters
    ----------
    stage_name   : Unique name for the stage (e.g. "sabr_calibration").
    config       : The experiment config dict (or sub-section) used by this stage.
    input_paths  : List of input files whose content should be included in the hash.
    """
    parts: dict = {"stage": stage_name, "config": config, "inputs": {}}
    for p in sorted(input_paths):
        p = Path(p)
        if p.exists():
            parts["inputs"][str(p)] = _hash_file(p)
    return _hash_value(parts)


class StageCache:
    """
    Persistent cache manifest stored as a JSON file inside the cache directory.

    Usage
    -----
    cache = StageCache(cache_dir)
    if cache.is_fresh("sabr_calibration", stage_hash):
        logger.info("Skipping sabr_calibration — cached")
    else:
        run_calibration(...)
        cache.record("sabr_calibration", stage_hash)
    """

    def __init__(self, cache_dir: Path | str) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.cache_dir / _MANIFEST
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> dict:
        if self._manifest_path.exists():
            with open(self._manifest_path) as fh:
                return json.load(fh)
        return {}

    def _save(self) -> None:
        with open(self._manifest_path, "w") as fh:
            json.dump(self._data, fh, indent=2)

    # ------------------------------------------------------------------
    def is_fresh(self, stage_name: str, stage_hash: str) -> bool:
        """Return True when the stored hash matches *stage_hash*."""
        return self._data.get(stage_name) == stage_hash

    def record(self, stage_name: str, stage_hash: str) -> None:
        """Persist *stage_hash* for *stage_name*."""
        self._data[stage_name] = stage_hash
        self._save()

    def invalidate(self, stage_name: str) -> None:
        """Force the next run of *stage_name* to re-execute."""
        self._data.pop(stage_name, None)
        self._save()

    def invalidate_all(self) -> None:
        self._data.clear()
        self._save()
