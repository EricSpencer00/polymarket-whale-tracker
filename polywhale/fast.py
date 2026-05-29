"""Fast-market detector — identify Polymarket's short-window crypto markets.

The "Up or Down" series prints a market for every 5/15/30/60-minute window
on each of Bitcoin / Ethereum / Solana / XRP / Dogecoin. Title shape:

    Bitcoin Up or Down - May 28, 9:25PM-9:30PM ET
    Ethereum Up or Down - May 28, 9:15PM-9:30PM ET

The window in minutes is end - start. We reject anything else; this filter
exists precisely so we can ignore the slow "Will Bitcoin reach $X by Y"
markets when hunting for scalper algos.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ASSETS = ("Bitcoin", "Ethereum", "Solana", "XRP", "Dogecoin")
# Polymarket uses "Up or Down" for the perpetuals-lite family. The trailing
# " ET" is consistent across the whole series.
_TITLE_RE = re.compile(
    r"^(?P<asset>"
    + "|".join(ASSETS)
    + r") Up or Down - [^,]+, (?P<sh>\d+):(?P<sm>\d+)(?P<sap>[AP])M-"
    r"(?P<eh>\d+):(?P<em>\d+)(?P<eap>[AP])M ET\s*$"
)

ALLOWED_WINDOWS = {5, 15, 30, 60}


@dataclass(frozen=True)
class FastMarket:
    asset: str
    window_minutes: int


def detect(question: str) -> Optional[FastMarket]:
    """Return FastMarket(asset, window_minutes) if `question` is a fast market.

    Returns None for any market outside the Up-or-Down family or with a
    window we don't consider 'fast'.
    """
    if not question:
        return None
    m = _TITLE_RE.match(question.strip())
    if not m:
        return None
    sh, sm, sap = int(m["sh"]), int(m["sm"]), m["sap"]
    eh, em, eap = int(m["eh"]), int(m["em"]), m["eap"]
    start = ((sh % 12) + (12 if sap == "P" else 0)) * 60 + sm
    end = ((eh % 12) + (12 if eap == "P" else 0)) * 60 + em
    window = end - start
    if window <= 0 or window not in ALLOWED_WINDOWS:
        return None
    return FastMarket(asset=m["asset"], window_minutes=window)


def is_fast(question: str) -> bool:
    return detect(question) is not None


def asset_symbol(asset_name: str) -> str:
    """Map a full asset name to its conventional ticker (Bitcoin -> BTC)."""
    return {
        "Bitcoin": "BTC", "Ethereum": "ETH", "Solana": "SOL",
        "XRP": "XRP", "Dogecoin": "DOGE",
    }.get(asset_name, asset_name.upper())
