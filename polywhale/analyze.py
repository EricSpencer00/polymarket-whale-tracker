"""Behavioral fingerprinter — score a wallet for algorithmic activity.

We pull a wallet's recent TRADE activity, compute a small set of features
that distinguish humans from bots, fold them into a 0–1 `algo_score`, and
pick a coarse style label. The thresholds are calibrated from the four
seed whales (W1, W2, boneweeper, purple-lamp-tree); they are deliberately
loose so the score grades rather than gates.

Features
--------
trades_per_min        Trade frequency over the observed window.
sub_second_frac       Fraction of consecutive trades with gap ≤ 1s.
sub_5s_frac           Fraction of consecutive trades with gap ≤ 5s.
buy_sell_ratio        BUY count / (BUY+SELL). 0 or 1 = entry-only/exit-only.
sizing_entropy        Shannon entropy of clip sizes bucketed by $5.
market_concentration  Share of trades on the most-traded conditionId.
both_sides_count      conditionIds where the wallet bought BOTH outcomes
                      within `BOTH_SIDES_WINDOW` seconds.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from polywhale.api import PolymarketPublicClient

BOTH_SIDES_WINDOW = 60  # seconds


@dataclass
class WalletFingerprint:
    address: str
    pseudonym: Optional[str]
    portfolio_value_usd: float
    n_trades: int
    window_seconds: int
    trades_per_min: float
    sub_second_frac: float
    sub_5s_frac: float
    buy_sell_ratio: float
    sizing_entropy: float
    distinct_markets: int
    market_concentration: float
    both_sides_count: int
    top_markets: List[Dict]
    algo_score: float
    style: str

    def summary(self) -> str:
        return (
            f"{self.address}  ({self.pseudonym or '—'})\n"
            f"  portfolio value:     ${self.portfolio_value_usd:,.2f}\n"
            f"  trades observed:     {self.n_trades} over {self.window_seconds}s\n"
            f"  trades/min:          {self.trades_per_min:.1f}\n"
            f"  sub-second clusters: {self.sub_second_frac:.0%}\n"
            f"  ≤5s clusters:        {self.sub_5s_frac:.0%}\n"
            f"  buy share:           {self.buy_sell_ratio:.0%}\n"
            f"  sizing entropy:      {self.sizing_entropy:.2f} bits\n"
            f"  distinct markets:    {self.distinct_markets}\n"
            f"  market concentration:{self.market_concentration:.0%}\n"
            f"  both-sides hedges:   {self.both_sides_count}\n"
            f"  algo_score:          {self.algo_score:.2f}\n"
            f"  style:               {self.style}"
        )


def fingerprint(
    client: PolymarketPublicClient,
    address: str,
    *,
    n_trades: int = 500,
) -> WalletFingerprint:
    address = address.lower()
    pv = client.portfolio_value(address)

    activity = client.activity(address, limit=n_trades, types=("TRADE",))
    activity = [a for a in activity if a.get("type") == "TRADE"]
    if not activity:
        return _empty_fingerprint(address, pv)

    pseudonym = next((a.get("pseudonym") for a in activity if a.get("pseudonym")), None)

    # Newest first per Polymarket convention; sort ascending for gap math.
    activity.sort(key=lambda a: int(a.get("timestamp") or 0))
    ts = [int(a.get("timestamp") or 0) for a in activity]
    span = max(1, ts[-1] - ts[0])
    gaps = [b - a for a, b in zip(ts, ts[1:])] if len(ts) > 1 else [0]

    sub_second = sum(1 for g in gaps if g <= 1) / max(1, len(gaps))
    sub_5s = sum(1 for g in gaps if g <= 5) / max(1, len(gaps))

    sides = [str(a.get("side", "")).upper() for a in activity]
    buys = sum(1 for s in sides if s == "BUY")
    sells = sum(1 for s in sides if s == "SELL")
    total_sided = max(1, buys + sells)
    buy_share = buys / total_sided

    sizes = [float(a.get("usdcSize") or 0.0) for a in activity]
    sizing_entropy = _bucket_entropy(sizes, bucket=5.0)

    market_counts: Counter = Counter()
    for a in activity:
        cid = a.get("conditionId")
        if cid:
            market_counts[cid] += 1
    distinct_markets = len(market_counts)
    most_common_count = market_counts.most_common(1)[0][1] if market_counts else 0
    market_concentration = most_common_count / max(1, len(activity))

    both_sides_count = _count_both_sides(activity, window_s=BOTH_SIDES_WINDOW)

    top_markets = []
    by_cid: Dict[str, List[dict]] = defaultdict(list)
    for a in activity:
        cid = a.get("conditionId")
        if cid:
            by_cid[cid].append(a)
    for cid, _count in market_counts.most_common(5):
        rows = by_cid[cid]
        top_markets.append({
            "condition_id": cid,
            "title": rows[0].get("title", ""),
            "slug": rows[0].get("slug", ""),
            "trades": len(rows),
            "usd_volume": round(sum(float(r.get("usdcSize") or 0) for r in rows), 2),
        })

    trades_per_min = len(activity) / max(1, span / 60.0)

    algo_score = _algo_score(
        trades_per_min=trades_per_min,
        sub_second=sub_second,
        sub_5s=sub_5s,
        market_concentration=market_concentration,
        both_sides_count=both_sides_count,
        distinct_markets=distinct_markets,
    )
    style = _style(
        algo_score=algo_score,
        buy_share=buy_share,
        market_concentration=market_concentration,
        both_sides_count=both_sides_count,
        distinct_markets=distinct_markets,
    )

    return WalletFingerprint(
        address=address,
        pseudonym=pseudonym,
        portfolio_value_usd=pv,
        n_trades=len(activity),
        window_seconds=span,
        trades_per_min=trades_per_min,
        sub_second_frac=sub_second,
        sub_5s_frac=sub_5s,
        buy_sell_ratio=buy_share,
        sizing_entropy=sizing_entropy,
        distinct_markets=distinct_markets,
        market_concentration=market_concentration,
        both_sides_count=both_sides_count,
        top_markets=top_markets,
        algo_score=algo_score,
        style=style,
    )


def _empty_fingerprint(address: str, pv: float) -> WalletFingerprint:
    return WalletFingerprint(
        address=address, pseudonym=None, portfolio_value_usd=pv,
        n_trades=0, window_seconds=0, trades_per_min=0.0,
        sub_second_frac=0.0, sub_5s_frac=0.0, buy_sell_ratio=0.0,
        sizing_entropy=0.0, distinct_markets=0, market_concentration=0.0,
        both_sides_count=0, top_markets=[], algo_score=0.0, style="inactive",
    )


def _bucket_entropy(values: List[float], bucket: float = 5.0) -> float:
    if not values:
        return 0.0
    counts: Counter = Counter(int(v // bucket) for v in values)
    n = sum(counts.values())
    return -sum((c / n) * math.log2(c / n) for c in counts.values() if c > 0)


def _count_both_sides(activity: List[dict], *, window_s: int) -> int:
    """Number of conditionIds where the wallet bought BOTH outcomes within window_s.

    'Both outcomes' = at least one BUY with outcomeIndex 0 and another with
    outcomeIndex 1 inside the same `window_s` rolling window. We collapse to
    a per-conditionId boolean so wallets that scalp the same market all day
    only count once.
    """
    by_cid: Dict[str, List[tuple]] = defaultdict(list)
    for a in activity:
        if str(a.get("side", "")).upper() != "BUY":
            continue
        cid = a.get("conditionId")
        if not cid:
            continue
        oi = a.get("outcomeIndex")
        ts = int(a.get("timestamp") or 0)
        if oi is None:
            continue
        by_cid[cid].append((ts, int(oi)))

    hits = 0
    for cid, rows in by_cid.items():
        rows.sort()
        seen_sides = {}
        # two-pointer sweep within window_s
        left = 0
        for right, (ts_r, side_r) in enumerate(rows):
            while rows[left][0] < ts_r - window_s:
                left += 1
            window_sides = {rows[i][1] for i in range(left, right + 1)}
            if 0 in window_sides and 1 in window_sides:
                hits += 1
                break
    return hits


def _algo_score(*, trades_per_min, sub_second, sub_5s, market_concentration,
                 both_sides_count, distinct_markets) -> float:
    """0–1 weighted combination of signals."""
    freq = _clamp(trades_per_min / 30.0)
    sub = 0.7 * sub_second + 0.3 * sub_5s
    conc = _clamp(market_concentration * 1.2)
    hedge = _clamp(both_sides_count / 5.0)
    breadth_penalty = _clamp(distinct_markets / 40.0)

    raw = (
        0.35 * freq +
        0.30 * sub +
        0.15 * conc +
        0.25 * hedge -
        0.10 * breadth_penalty
    )
    return max(0.0, min(1.0, raw))


def _style(*, algo_score, buy_share, market_concentration, both_sides_count,
           distinct_markets) -> str:
    if algo_score < 0.25 and distinct_markets > 10:
        return "swing"
    if algo_score < 0.25:
        return "degen"
    if both_sides_count >= 3 and buy_share > 0.85:
        return "hf_scalper_straddle"
    if 0.35 <= buy_share <= 0.65 and market_concentration > 0.5:
        return "market_maker"
    if buy_share > 0.85 and algo_score >= 0.4:
        return "hf_scalper_entry"
    return "directional_algo"


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))
