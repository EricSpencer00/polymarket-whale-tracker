"""Unit tests for the offline analyzer math.

The internet-touching modules are exercised via the CLI smoketest scripts;
here we only test pure functions on synthetic trade rows.
"""
from __future__ import annotations

from polywhale.analyze import (
    _algo_score,
    _bucket_entropy,
    _count_both_sides,
    _style,
)


def _trade(ts, cid="C1", outcome_index=0, side="BUY", usd=10.0):
    return {
        "timestamp": ts,
        "conditionId": cid,
        "outcomeIndex": outcome_index,
        "side": side,
        "usdcSize": usd,
        "type": "TRADE",
    }


def test_bucket_entropy_uniform_high():
    sizes = [5, 15, 25, 35, 45, 55]  # all in different buckets of $5
    assert _bucket_entropy(sizes, bucket=5.0) > 2.0


def test_bucket_entropy_constant_zero():
    sizes = [10, 10, 10, 10]
    assert _bucket_entropy(sizes, bucket=5.0) == 0.0


def test_count_both_sides_detects_hedge_in_window():
    activity = [
        _trade(1000, cid="C1", outcome_index=0),
        _trade(1010, cid="C1", outcome_index=1),  # same market, opposite side, within 60s
        _trade(1100, cid="C2", outcome_index=0),
    ]
    assert _count_both_sides(activity, window_s=60) == 1


def test_count_both_sides_window_too_wide():
    activity = [
        _trade(1000, cid="C1", outcome_index=0),
        _trade(1100, cid="C1", outcome_index=1),  # 100s apart > 60s window
    ]
    assert _count_both_sides(activity, window_s=60) == 0


def test_count_both_sides_ignores_sells():
    activity = [
        _trade(1000, cid="C1", outcome_index=0, side="BUY"),
        _trade(1010, cid="C1", outcome_index=1, side="SELL"),
    ]
    assert _count_both_sides(activity, window_s=60) == 0


def test_algo_score_hf_pattern():
    # 90 trades/min, 70% sub-second, concentrated in one market, with hedges
    s = _algo_score(
        trades_per_min=90.0,
        sub_second=0.7,
        sub_5s=0.95,
        market_concentration=0.6,
        both_sides_count=5,
        distinct_markets=3,
    )
    assert s > 0.6


def test_algo_score_human_pattern():
    s = _algo_score(
        trades_per_min=0.5,
        sub_second=0.0,
        sub_5s=0.0,
        market_concentration=0.1,
        both_sides_count=0,
        distinct_markets=4,
    )
    assert s < 0.15


def test_style_market_maker():
    s = _style(algo_score=0.5, buy_share=0.5, market_concentration=0.7,
               both_sides_count=0, distinct_markets=3)
    assert s == "market_maker"


def test_style_straddle_scalper():
    s = _style(algo_score=0.7, buy_share=0.95, market_concentration=0.4,
               both_sides_count=4, distinct_markets=10)
    assert s == "hf_scalper_straddle"


def test_style_degen():
    s = _style(algo_score=0.1, buy_share=1.0, market_concentration=0.1,
               both_sides_count=0, distinct_markets=2)
    assert s == "degen"


def test_style_swing():
    s = _style(algo_score=0.1, buy_share=0.6, market_concentration=0.1,
               both_sides_count=0, distinct_markets=20)
    assert s == "swing"
