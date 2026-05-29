"""Conviction scoring — how confident is this wallet's sizing?

Edge is captured by PnL trajectory (polywhale.pnl). Conviction is the
*other* half: a wallet might be net-positive while spraying $0.05 clips,
and that's not worth copying — the slippage cost on the mirror trade
alone will eat the edge. We want wallets that bet in size *and* with
recognizable algorithmic regularity.

Metrics
-------
median_clip_usd     Median per-trade USDC size.
p90_clip_usd        90th-percentile clip.
max_clip_usd        Biggest single clip seen.
repeat_size_ratio   Fraction of clips within ±20% of the median —
                    high values mean the wallet is bucketing into a
                    fixed clip size (classic algo behavior).
size_entropy_bits   Shannon entropy of $5-bucketed clip sizes; low
                    entropy = repeats one or two sizes.
conviction_score    0–1 composite. Weights:
                       0.55 * median_clip_score   (size matters most)
                       0.30 * repeat_size_ratio   (regularity)
                       0.15 * size_consistency    (low entropy)
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ConvictionProfile:
    median_clip_usd: float
    p90_clip_usd: float
    max_clip_usd: float
    repeat_size_ratio: float
    size_entropy_bits: float
    conviction_score: float


def from_clips(clips: List[float]) -> ConvictionProfile:
    if not clips:
        return ConvictionProfile(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    clips_sorted = sorted(clips)
    n = len(clips_sorted)
    med = clips_sorted[n // 2] if n % 2 else 0.5 * (
        clips_sorted[n // 2 - 1] + clips_sorted[n // 2]
    )
    p90 = clips_sorted[int(0.9 * (n - 1))]
    mx = clips_sorted[-1]

    repeat = (
        sum(1 for c in clips if 0.8 * med <= c <= 1.2 * med) / n
        if med > 0 else 0.0
    )

    entropy = _bucket_entropy(clips, bucket=5.0)
    # consistency: lower entropy -> higher score. Cap at 4 bits.
    consistency = max(0.0, 1.0 - min(4.0, entropy) / 4.0)

    # Saturating size scores: median $50 -> full credit; clip ramp slower.
    size_score = _clamp(med / 50.0)

    conviction = 0.55 * size_score + 0.30 * repeat + 0.15 * consistency

    return ConvictionProfile(
        median_clip_usd=med,
        p90_clip_usd=p90,
        max_clip_usd=mx,
        repeat_size_ratio=repeat,
        size_entropy_bits=entropy,
        conviction_score=_clamp(conviction),
    )


def _bucket_entropy(values: List[float], bucket: float) -> float:
    if not values:
        return 0.0
    counts: Counter = Counter(int(v // bucket) for v in values)
    n = sum(counts.values())
    return -sum((c / n) * math.log2(c / n) for c in counts.values() if c > 0)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))
