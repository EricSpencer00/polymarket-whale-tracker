"""PnL trajectory reconstruction — no native endpoint, so we replay events.

Polymarket has no per-wallet PnL-history endpoint. We rebuild it from the
public /activity feed. The realized-cashflow accounting is:

    TRADE BUY    cashflow  -= usdcSize           (paid USDC for shares)
    TRADE SELL   cashflow  += usdcSize           (received USDC for shares)
    REDEEM       cashflow  += usdcSize           (winning ticket paid at $1)
    SPLIT        cashflow  -= usdcSize           (locked USDC into YES + NO pair)
    MERGE        cashflow  += usdcSize           (unlocked USDC from YES + NO pair)
    REWARD       cashflow  += usdcSize           (LP / referral / promo credit)

Cumulative cashflow at time T is the wallet's realized PnL through T,
treating unredeemed positions as still in flight. For a steady-positive
algo, this curve should be near-monotonically up with small drawdowns.

We bucket per UTC day, fit a line to the cumulative series, and emit:

    slope          USD gained per day on average (linear fit)
    r_squared      proportion of variance explained by the linear trend
    max_drawdown   worst peak-to-trough on the cumulative curve, USD
    winning_days   fraction of days with positive net cashflow
    steadiness     0..1 composite (slope_positive * r2 * (1 - dd_ratio))
"""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple  # noqa: F401

from polywhale.api import PolymarketPublicClient

log = logging.getLogger(__name__)

REALIZING_TYPES = ("TRADE", "REDEEM", "SPLIT", "MERGE", "REWARD")


@dataclass
class PnLSeries:
    address: str
    bucket: str = "day"                                  # "day" or "hour"
    days: List[str] = field(default_factory=list)        # ISO timestamps per bucket
    daily_pnl: List[float] = field(default_factory=list)
    cumulative_pnl: List[float] = field(default_factory=list)
    n_events: int = 0
    span_days: float = 0.0
    total_realized_pnl: float = 0.0
    slope_usd_per_day: float = 0.0
    r_squared: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_ratio: float = 0.0
    winning_day_rate: float = 0.0
    steadiness: float = 0.0
    verdict: str = ""

    def summary(self) -> str:
        bucket_word = "hour" if self.bucket == "hour" else "day"
        return (
            f"{self.address}\n"
            f"  events:               {self.n_events}\n"
            f"  span:                 {self.span_days:.1f} days "
            f"({self.days[0] if self.days else '—'} .. "
            f"{self.days[-1] if self.days else '—'})\n"
            f"  bucket:               {self.bucket}\n"
            f"  realized PnL (USDC):  ${self.total_realized_pnl:,.2f}\n"
            f"  slope (USD/day):      ${self.slope_usd_per_day:,.2f}\n"
            f"  R^2 (trend fit):      {self.r_squared:.2f}\n"
            f"  max drawdown:         ${self.max_drawdown:,.2f} "
            f"({self.max_drawdown_ratio:.0%} of peak)\n"
            f"  winning {bucket_word}s:         {self.winning_day_rate:.0%}\n"
            f"  steadiness (0–1):     {self.steadiness:.2f}\n"
            f"  verdict:              {self.verdict}"
        )


def reconstruct(
    client: PolymarketPublicClient,
    wallet: str,
    *,
    days_back: int = 30,
    max_events: int = 10000,
) -> PnLSeries:
    """Pull activity, bucket by UTC day, compute steadiness metrics."""
    cutoff = int(time.time() - days_back * 86400) if days_back > 0 else 0
    events = _pull_events(client, wallet, cutoff=cutoff, max_events=max_events)

    series = PnLSeries(address=wallet.lower())
    if not events:
        series.verdict = "no activity"
        return series

    # Decide bucket size based on calendar span. Short-history wallets get
    # hourly buckets so the steadiness math still has signal.
    ts_min = min(int(e.get("timestamp") or 0) for e in events)
    ts_max = max(int(e.get("timestamp") or 0) for e in events)
    span_seconds = max(1, ts_max - ts_min)
    span_days = span_seconds / 86400.0
    bucket = "day" if span_days >= 3.0 else "hour"

    by_bucket: Dict[str, float] = defaultdict(float)
    for e in events:
        ts = int(e.get("timestamp") or 0)
        if ts <= 0:
            continue
        if bucket == "day":
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H")
        by_bucket[label] += _signed_cashflow(e)

    if not by_bucket:
        series.verdict = "no realizing events"
        return series

    labels = sorted(by_bucket.keys())
    bucket_pnl = [by_bucket[d] for d in labels]
    cum: List[float] = []
    running = 0.0
    for v in bucket_pnl:
        running += v
        cum.append(running)

    series.bucket = bucket
    series.days = labels
    series.daily_pnl = bucket_pnl
    series.cumulative_pnl = cum
    series.n_events = len(events)
    series.span_days = round(span_days, 2)
    series.total_realized_pnl = cum[-1]

    slope, r2 = _linfit(cum)
    # Normalize slope to USD/day regardless of bucket size for cross-wallet comparison.
    buckets_per_day = 24.0 if bucket == "hour" else 1.0
    series.slope_usd_per_day = slope * buckets_per_day
    series.r_squared = r2
    series.max_drawdown, series.max_drawdown_ratio = _max_drawdown(cum)
    series.winning_day_rate = sum(1 for v in bucket_pnl if v > 0) / len(bucket_pnl)

    series.steadiness = _steadiness(
        slope=series.slope_usd_per_day, r2=r2, dd_ratio=series.max_drawdown_ratio,
        winning_day_rate=series.winning_day_rate,
        total_pnl=series.total_realized_pnl,
    )
    series.verdict = _verdict(series)

    return series


# ----------------------------------------------------------------
# event ingestion
# ----------------------------------------------------------------
def _pull_events(
    client: PolymarketPublicClient,
    wallet: str,
    *,
    cutoff: int,
    max_events: int,
) -> List[dict]:
    """Page /activity by walking backward in time with `end=<oldest_ts>`.

    data-api hard-caps `offset` at 3000; for high-frequency wallets that's
    only a few hours of history. Instead of paginating by offset we paginate
    by sliding the `end=` parameter to one second before the oldest event
    we've seen so far. This walks arbitrarily far back without ever needing
    a large offset.
    """
    out: List[dict] = []
    page_size = 500
    end_cursor: Optional[int] = None
    seen_hashes: set = set()
    max_pages = 60  # 60 * 500 = 30k events ceiling

    for _ in range(max_pages):
        if len(out) >= max_events:
            break
        page = client.activity(
            wallet,
            limit=page_size, offset=0,
            types=REALIZING_TYPES,
            start=cutoff if cutoff > 0 else None,
            end=end_cursor,
        )
        if not page:
            break
        # Dedupe in case the boundary event repeats across pages.
        new = []
        for e in page:
            key = e.get("transactionHash") or (e.get("type"), e.get("timestamp"),
                                                e.get("asset"), e.get("size"))
            if key in seen_hashes:
                continue
            seen_hashes.add(key)
            new.append(e)
        if not new:
            break
        out.extend(new)
        oldest = min(int(e.get("timestamp") or 0) for e in new)
        if oldest <= cutoff > 0:
            break
        # Walk one second back so we don't re-fetch the boundary event.
        next_end = oldest - 1
        if end_cursor is not None and next_end >= end_cursor:
            break  # not making progress
        end_cursor = next_end
        if len(page) < page_size:
            break
    return out


def _signed_cashflow(ev: dict) -> float:
    """Convert one activity event into a signed USDC delta."""
    t = (ev.get("type") or "").upper()
    usd = _to_float(ev.get("usdcSize"))
    if usd == 0.0:
        # Fall back to price*size where usdcSize is absent (older events).
        usd = _to_float(ev.get("price")) * _to_float(ev.get("size"))
    if t == "TRADE":
        side = (ev.get("side") or "").upper()
        if side == "BUY":
            return -usd
        if side == "SELL":
            return +usd
        return 0.0
    if t == "SPLIT":
        return -usd
    if t == "MERGE":
        return +usd
    if t == "REDEEM":
        return +usd
    if t == "REWARD":
        return +usd
    return 0.0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------
# steadiness math
# ----------------------------------------------------------------
def _linfit(y: List[float]) -> Tuple[float, float]:
    """Least-squares fit y ~ a*x + b; return (slope, R^2)."""
    n = len(y)
    if n < 2:
        return 0.0, 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(y) / n
    sxy = sum((xs[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    sxx = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if sxx == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    if ss_tot == 0:
        return slope, 1.0
    ss_res = sum((y[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    r2 = max(0.0, 1.0 - ss_res / ss_tot)
    return slope, r2


def _max_drawdown(cum: List[float]) -> Tuple[float, float]:
    """Worst peak-to-trough drop in absolute USD and as ratio of peak."""
    if not cum:
        return 0.0, 0.0
    peak = cum[0]
    max_dd = 0.0
    max_dd_ratio = 0.0
    for v in cum:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
            if peak > 0:
                max_dd_ratio = dd / peak
    return max_dd, max_dd_ratio


def _steadiness(*, slope, r2, dd_ratio, winning_day_rate, total_pnl) -> float:
    """0..1 composite for ranking wallets as steady winners."""
    if total_pnl <= 0:
        return 0.0
    slope_score = _clamp(slope / 50.0)  # >$50/day -> full credit
    r2_score = _clamp(r2)
    dd_score = _clamp(1.0 - dd_ratio)
    win_score = _clamp(winning_day_rate)
    return 0.35 * slope_score + 0.30 * r2_score + 0.20 * dd_score + 0.15 * win_score


def _verdict(s: PnLSeries) -> str:
    if s.total_realized_pnl <= 0:
        return "net loss — skip"
    if len(s.days) < 4:
        return "insufficient history"
    if s.steadiness >= 0.55 and s.r_squared >= 0.7:
        return "steady positive"
    if s.steadiness >= 0.35:
        return "noisy positive"
    return "positive but erratic"


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))
