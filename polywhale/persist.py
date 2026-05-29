"""Watchlist persistence — write the qualified watchlist to disk.

By default we write into `$POLYWHALE_DATA_DIR` (default `./data/`),
which is gitignored. Each save is timestamped; a `latest_<asset>.json`
symlink (or copy on platforms without symlinks) always points at the
newest file so downstream tools can load without parsing dates.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("./data")


def data_dir() -> Path:
    p = Path(os.environ.get("POLYWHALE_DATA_DIR") or DEFAULT_DATA_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_watchlist(
    asset: str,
    qualified: List[Dict[str, Any]],
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write the watchlist and update the `latest_<asset>.json` pointer."""
    d = data_dir()
    ts = int(time.time())
    payload = {
        "asset": asset.upper(),
        "generated_at": ts,
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "extra": extra or {},
        "wallets": qualified,
    }
    path = d / f"{asset.lower()}-whales-{ts}.json"
    path.write_text(json.dumps(payload, indent=2))
    latest = d / f"latest_{asset.lower()}.json"
    latest.write_text(json.dumps(payload, indent=2))
    log.info("watchlist saved -> %s (%d wallets)", path, len(qualified))
    return path


def load_latest(asset: str) -> Optional[Dict[str, Any]]:
    p = data_dir() / f"latest_{asset.lower()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        log.warning("failed to load %s: %s", p, e)
        return None
