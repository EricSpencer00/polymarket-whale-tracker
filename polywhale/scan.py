"""Fast-market volume scanner — find the heaviest traders in 5m/15m crypto markets.

Discovery via /holders misses scalpers like W2 because their positions
evaporate every five minutes. Instead we enumerate recent fast markets
for the chosen asset, pull `/trades?market=<conditionId>` for each, and
aggregate USDC volume per wallet across the window.

This is the right surface for finding "BTC scalper whales": wallets that
move a lot of dollars through the order book without ever sitting on
a big point-in-time position.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from polywhale.api import PolymarketPublicClient
from polywhale.discover import CRYPTO_TAG_ID, _question_matches_asset
from polywhale.fast import detect as detect_fast

log = logging.getLogger(__name__)


@dataclass
class TraderTally:
    proxy_wallet: str
    pseudonym: Optional[str] = None
    name: Optional[str] = None
    usdc_volume: float = 0.0
    trade_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    distinct_markets: int = 0
    market_ids: set = field(default_factory=set)
    last_seen: int = 0
    clips: List[float] = field(default_factory=list)

    def record(self, trade: dict, market_cid: str) -> None:
        try:
            price = float(trade.get("price") or 0.0)
            size = float(trade.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        usd = float(trade.get("usdcSize") or (price * size))
        side = str(trade.get("side", "")).upper()
        ts = int(trade.get("timestamp") or 0)
        self.usdc_volume += usd
        self.trade_count += 1
        if usd > 0:
            self.clips.append(usd)
        if side == "BUY":
            self.buy_volume += usd
        elif side == "SELL":
            self.sell_volume += usd
        if market_cid not in self.market_ids:
            self.market_ids.add(market_cid)
            self.distinct_markets += 1
        if ts > self.last_seen:
            self.last_seen = ts
        self.pseudonym = self.pseudonym or trade.get("pseudonym")
        self.name = self.name or trade.get("name")

    @property
    def buy_share(self) -> float:
        total = self.buy_volume + self.sell_volume
        return (self.buy_volume / total) if total else 0.0

    @property
    def minutes_since_last_trade(self) -> float:
        import time as _time
        if not self.last_seen:
            return float("inf")
        return max(0.0, (_time.time() - self.last_seen) / 60.0)


def scan_fast_markets(
    client: PolymarketPublicClient,
    *,
    asset: str = "BTC",
    window_hours: float = 6.0,
    market_cap: int = 80,
    trades_per_market: int = 1000,
    include_closed: bool = True,
) -> Dict[str, TraderTally]:
    """Aggregate trade volume per wallet across recent fast markets.

    window_hours bounds how far back into trade history we accept events.
    market_cap bounds how many distinct markets we touch (~1 req each).
    """
    markets = _list_recent_fast_markets(
        client, asset=asset, market_cap=market_cap, include_closed=include_closed,
        look_back_hours=window_hours, look_ahead_hours=1.0,
    )
    log.info("scan: %d fast %s markets discovered", len(markets), asset)

    cutoff = int(time.time() - window_hours * 3600)
    tallies: Dict[str, TraderTally] = defaultdict(lambda: TraderTally(proxy_wallet=""))

    for m in markets:
        cid = m["conditionId"]
        try:
            trades = client._get(  # noqa: SLF001
                "https://data-api.polymarket.com",
                "/trades",
                {"market": cid, "limit": trades_per_market},
            ) or []
        except Exception as e:
            log.warning("trades(%s) failed: %s", cid, e)
            continue
        for t in trades:
            ts = int(t.get("timestamp") or 0)
            if ts < cutoff:
                continue
            wallet = (t.get("proxyWallet") or "").lower()
            if not wallet:
                continue
            tally = tallies[wallet]
            if not tally.proxy_wallet:
                tally.proxy_wallet = wallet
            tally.record(t, cid)

    return tallies


def top_traders(
    tallies: Dict[str, TraderTally],
    *,
    min_volume_usd: float = 1000.0,
    limit: int = 25,
) -> List[TraderTally]:
    out = [t for t in tallies.values() if t.usdc_volume >= min_volume_usd]
    out.sort(key=lambda t: t.usdc_volume, reverse=True)
    return out[:limit]


def _list_recent_fast_markets(
    client: PolymarketPublicClient,
    *,
    asset: str,
    market_cap: int,
    include_closed: bool,
    look_back_hours: float = 6.0,
    look_ahead_hours: float = 1.0,
) -> List[dict]:
    """Find fast markets near the current moment.

    Fast 5/15m markets have minuscule 24h volume each (they resolve and die
    in minutes), so we can't order by volume — we order by endDate. Two
    passes:

      A. Recently-closed   closed=true,  order=endDate desc -> latest first
      B. About-to-resolve  closed=false, order=endDate asc  -> soonest first

    We page each until endDate falls outside [-look_back_hours, +look_ahead_hours]
    or the market cap is reached. Title fast-detect filters out slow markets.
    """
    now = time.time()
    cutoff_past = now - look_back_hours * 3600
    cutoff_future = now + look_ahead_hours * 3600

    out: List[dict] = []
    seen_ids: set = set()

    def collect(rows: List[dict]) -> bool:
        """Append matching markets; return True if we should stop paging."""
        stop = False
        for raw in rows:
            m = client.parse_gamma_market(raw)
            cid = m.get("conditionId")
            if not cid or cid in seen_ids:
                continue
            q = m.get("question") or ""
            if not _question_matches_asset(q, asset):
                continue
            if detect_fast(q) is None:
                continue
            end_ts = _iso_to_epoch(m.get("endDate")) if m.get("endDate") else None
            if end_ts is not None and not (cutoff_past <= end_ts <= cutoff_future):
                # Outside our window — once we see one we can stop paging
                # in this pass since results are endDate-ordered.
                stop = True
                continue
            seen_ids.add(cid)
            out.append(m)
            if len(out) >= market_cap:
                return True
        return stop

    page_size = 100
    max_pages = 25

    if include_closed:
        offset = 0
        for _ in range(max_pages):
            rows = client.search_markets(
                tag_id=CRYPTO_TAG_ID, closed=True, active=None,
                order="endDate", ascending=False,
                limit=page_size, offset=offset,
            )
            if not rows:
                break
            stop = collect(rows)
            if stop or len(rows) < page_size or len(out) >= market_cap:
                break
            offset += page_size

    if len(out) < market_cap:
        offset = 0
        for _ in range(max_pages):
            rows = client.search_markets(
                tag_id=CRYPTO_TAG_ID, closed=False, active=True,
                order="endDate", ascending=True,
                limit=page_size, offset=offset,
            )
            if not rows:
                break
            stop = collect(rows)
            if stop or len(rows) < page_size or len(out) >= market_cap:
                break
            offset += page_size

    return out


def _iso_to_epoch(iso: str) -> Optional[float]:
    from datetime import datetime, timezone
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None
