"""Macro snapshot — what's the BTC market landscape look like right now?

For a given asset (BTC by default) we list the active markets, ordered by
24h volume, and decorate each with midpoint and time-to-resolution. This
is the cheap context layer a copy-trade consumer wants to glance at before
acting on a signal: is the market thin? about to resolve? deeply skewed?
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from polywhale.api import PolymarketPublicClient
from polywhale.discover import CRYPTO_TAG_ID, _question_matches_asset, _to_float

log = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    condition_id: str
    slug: str
    question: str
    end_date: Optional[str]
    minutes_to_close: Optional[float]
    volume_24h: float
    liquidity: float
    outcomes: List[str]
    outcome_prices: List[float]
    midpoint_yes: Optional[float]

    def yes_no_skew(self) -> Optional[float]:
        """abs(yes - no) — closer to 0 = balanced market; closer to 1 = lopsided."""
        if len(self.outcome_prices) < 2:
            return None
        return abs(self.outcome_prices[0] - self.outcome_prices[1])


def macro_snapshot(
    client: PolymarketPublicClient,
    *,
    asset: str = "BTC",
    limit: int = 25,
    include_midpoint: bool = False,
) -> List[MarketSnapshot]:
    seen: set = set()
    out: List[MarketSnapshot] = []

    page_size = 100
    offset = 0
    max_pages = 6
    for _ in range(max_pages):
        rows = client.search_markets(
            tag_id=CRYPTO_TAG_ID,
            active=True, closed=False,
            order="volume24hr", ascending=False,
            limit=page_size, offset=offset,
        )
        if not rows:
            break
        for raw in rows:
            m = client.parse_gamma_market(raw)
            cid = m.get("conditionId")
            if not cid or cid in seen:
                continue
            if not _question_matches_asset(m.get("question", ""), asset):
                continue
            seen.add(cid)

            prices = [_to_float(p, 0.0) for p in (m.get("outcomePrices") or [])]
            outcomes = m.get("outcomes") or []
            end = m.get("endDate")
            ttc = _minutes_until(end)

            mid: Optional[float] = None
            if include_midpoint:
                tids = m.get("clobTokenIds") or []
                if tids:
                    try:
                        mid = client.midpoint(tids[0])
                    except Exception as e:
                        log.debug("midpoint %s failed: %s", tids[0], e)

            out.append(MarketSnapshot(
                condition_id=cid,
                slug=m.get("slug", ""),
                question=m.get("question", ""),
                end_date=end,
                minutes_to_close=ttc,
                volume_24h=_to_float(m.get("volume24hr"), 0.0),
                liquidity=_to_float(m.get("liquidity"), 0.0),
                outcomes=outcomes,
                outcome_prices=prices,
                midpoint_yes=mid,
            ))
            if len(out) >= limit:
                break
        if len(out) >= limit or len(rows) < page_size:
            break
        offset += page_size

    out.sort(key=lambda s: s.volume_24h, reverse=True)
    return out[:limit]


def _minutes_until(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        # ISO8601 with trailing Z
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.timestamp() - time.time()) / 60.0
    except ValueError:
        return None
