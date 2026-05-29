"""Autopilot — find the BTC whales worth copying, then stream their trades.

One command, end-to-end:

    1. scan-fast      rank wallets by USDC volume in recent fast markets
    2. conviction     drop wallets with tiny clips / sprayed sizing
    3. pnl            drop wallets without provably-positive realized PnL
    4. recency        drop wallets that haven't traded in the last N minutes
    5. persist        save the qualified watchlist to data/latest_<asset>.json
    6. watch          open WSS, stream CopySignals tagged with each wallet's
                      qualified metrics

The output of the qualification stage is itself useful (you can stop after
step 5 with --no-watch) and it's deterministic given the same time window.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from polywhale.api import PolymarketPublicClient
from polywhale.conviction import ConvictionProfile, from_clips
from polywhale.persist import save_watchlist
from polywhale.pnl import PnLSeries, reconstruct as reconstruct_pnl
from polywhale.scan import TraderTally, scan_fast_markets, top_traders

log = logging.getLogger(__name__)


@dataclass
class QualifiedWhale:
    proxy_wallet: str
    pseudonym: Optional[str]
    name: Optional[str]
    scan_volume_usd: float
    scan_trades: int
    scan_markets: int
    buy_share: float
    minutes_since_last_trade: float
    conviction: ConvictionProfile
    pnl: PnLSeries

    def to_dict(self) -> dict:
        return {
            "proxy_wallet": self.proxy_wallet,
            "pseudonym": self.pseudonym,
            "name": self.name,
            "scan_volume_usd": round(self.scan_volume_usd, 2),
            "scan_trades": self.scan_trades,
            "scan_markets": self.scan_markets,
            "buy_share": round(self.buy_share, 3),
            "minutes_since_last_trade": round(self.minutes_since_last_trade, 1),
            "conviction": asdict(self.conviction),
            "pnl": {
                "total_realized_pnl": round(self.pnl.total_realized_pnl, 2),
                "slope_usd_per_day": round(self.pnl.slope_usd_per_day, 2),
                "r_squared": round(self.pnl.r_squared, 3),
                "max_drawdown": round(self.pnl.max_drawdown, 2),
                "max_drawdown_ratio": round(self.pnl.max_drawdown_ratio, 3),
                "winning_day_rate": round(self.pnl.winning_day_rate, 3),
                "steadiness": round(self.pnl.steadiness, 3),
                "verdict": self.pnl.verdict,
                "bucket": self.pnl.bucket,
                "span_days": self.pnl.span_days,
                "n_events": self.pnl.n_events,
            },
            "style_hint": self.style_hint(),
        }

    def style_hint(self) -> str:
        c = self.conviction.conviction_score
        s = self.pnl.steadiness
        if c >= 0.55 and s >= 0.55:
            return "steady_high_conviction"
        if s >= 0.55:
            return "steady_low_conviction"
        if c >= 0.55:
            return "high_conviction_volatile"
        return "marginal"


@dataclass
class QualifyParams:
    asset: str = "BTC"
    hours: float = 6.0
    markets: int = 80
    candidates: int = 30
    days: int = 7
    min_volume_usd: float = 1500.0
    min_pnl_usd: float = 0.0
    min_steadiness: float = 0.45
    min_conviction: float = 0.20
    min_median_clip_usd: float = 0.0
    max_minutes_since_trade: float = 240.0  # 4h


def qualify_whales(
    client: PolymarketPublicClient,
    params: QualifyParams,
) -> List[QualifiedWhale]:
    """Run the full qualification pipeline; return ranked qualified wallets."""
    log.info("scan: asset=%s window_hours=%.1f markets<=%d",
             params.asset, params.hours, params.markets)
    tallies = scan_fast_markets(
        client,
        asset=params.asset,
        window_hours=params.hours,
        market_cap=params.markets,
    )
    candidates = top_traders(
        tallies, min_volume_usd=params.min_volume_usd, limit=params.candidates,
    )
    log.info("candidates: %d wallets pass volume gate", len(candidates))

    out: List[QualifiedWhale] = []
    for tally in candidates:
        if tally.minutes_since_last_trade > params.max_minutes_since_trade:
            log.debug("skip %s: last_trade %.0fmin ago",
                      tally.proxy_wallet, tally.minutes_since_last_trade)
            continue
        conviction = from_clips(tally.clips)
        if (conviction.conviction_score < params.min_conviction or
                conviction.median_clip_usd < params.min_median_clip_usd):
            continue
        try:
            series = reconstruct_pnl(
                client, tally.proxy_wallet,
                days_back=params.days,
            )
        except Exception as e:
            log.warning("pnl(%s) failed: %s", tally.proxy_wallet, e)
            continue
        if (series.total_realized_pnl < params.min_pnl_usd or
                series.steadiness < params.min_steadiness):
            continue
        out.append(QualifiedWhale(
            proxy_wallet=tally.proxy_wallet,
            pseudonym=tally.pseudonym,
            name=tally.name,
            scan_volume_usd=tally.usdc_volume,
            scan_trades=tally.trade_count,
            scan_markets=tally.distinct_markets,
            buy_share=tally.buy_share,
            minutes_since_last_trade=tally.minutes_since_last_trade,
            conviction=conviction,
            pnl=series,
        ))

    # Rank by a blended score: 60% PnL steadiness + 40% conviction.
    out.sort(key=lambda q: 0.6 * q.pnl.steadiness + 0.4 * q.conviction.conviction_score,
             reverse=True)
    return out


def persist(asset: str, qualified: List[QualifiedWhale], params: QualifyParams):
    return save_watchlist(
        asset,
        [q.to_dict() for q in qualified],
        extra={
            "params": {
                "hours": params.hours, "days": params.days,
                "markets": params.markets, "candidates": params.candidates,
                "min_volume_usd": params.min_volume_usd,
                "min_pnl_usd": params.min_pnl_usd,
                "min_steadiness": params.min_steadiness,
                "min_conviction": params.min_conviction,
                "min_median_clip_usd": params.min_median_clip_usd,
                "max_minutes_since_trade": params.max_minutes_since_trade,
            },
        },
    )


def style_hint_map(qualified: List[QualifiedWhale]) -> Dict[str, str]:
    return {q.proxy_wallet: q.style_hint() for q in qualified}
