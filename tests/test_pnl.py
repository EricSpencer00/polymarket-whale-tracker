from polywhale.pnl import (
    PnLSeries,
    _linfit,
    _max_drawdown,
    _signed_cashflow,
    _steadiness,
    _verdict,
)


def test_signed_cashflow_buy():
    assert _signed_cashflow({"type": "TRADE", "side": "BUY", "usdcSize": 100}) == -100


def test_signed_cashflow_sell():
    assert _signed_cashflow({"type": "TRADE", "side": "SELL", "usdcSize": 100}) == +100


def test_signed_cashflow_redeem():
    assert _signed_cashflow({"type": "REDEEM", "usdcSize": 50}) == +50


def test_signed_cashflow_split_negative():
    assert _signed_cashflow({"type": "SPLIT", "usdcSize": 75}) == -75


def test_signed_cashflow_merge_positive():
    assert _signed_cashflow({"type": "MERGE", "usdcSize": 75}) == +75


def test_signed_cashflow_reward():
    assert _signed_cashflow({"type": "REWARD", "usdcSize": 5}) == +5


def test_signed_cashflow_unknown_type_zero():
    assert _signed_cashflow({"type": "UNKNOWN", "usdcSize": 100}) == 0


def test_signed_cashflow_falls_back_to_price_times_size():
    ev = {"type": "TRADE", "side": "BUY", "price": "0.5", "size": "10"}
    assert _signed_cashflow(ev) == -5.0


def test_linfit_monotonic_increase_high_r2():
    slope, r2 = _linfit([0.0, 10.0, 20.0, 30.0, 40.0])
    assert slope == 10.0
    assert r2 > 0.99


def test_linfit_flat_zero_slope():
    slope, r2 = _linfit([5.0, 5.0, 5.0, 5.0])
    assert slope == 0.0


def test_linfit_short_series():
    assert _linfit([42.0]) == (0.0, 0.0)


def test_max_drawdown_no_drawdown():
    dd, ratio = _max_drawdown([0.0, 10.0, 20.0, 30.0])
    assert dd == 0.0
    assert ratio == 0.0


def test_max_drawdown_peak_then_trough():
    dd, ratio = _max_drawdown([0.0, 100.0, 60.0, 80.0])
    assert dd == 40.0
    assert abs(ratio - 0.4) < 1e-9


def test_steadiness_zero_when_negative_pnl():
    s = _steadiness(slope=5, r2=0.9, dd_ratio=0.1,
                     winning_day_rate=0.8, total_pnl=-1.0)
    assert s == 0.0


def test_steadiness_high_for_clean_winner():
    s = _steadiness(slope=50, r2=0.95, dd_ratio=0.05,
                     winning_day_rate=0.9, total_pnl=1000)
    assert s > 0.85


def test_verdict_skip_on_loss():
    s = PnLSeries(address="0x", total_realized_pnl=-100)
    assert _verdict(s) == "net loss — skip"


def test_verdict_steady_positive():
    s = PnLSeries(address="0x", total_realized_pnl=500, span_days=14,
                   days=list("abcdef"),
                   steadiness=0.6, r_squared=0.8)
    assert _verdict(s) == "steady positive"


def test_verdict_noisy_positive():
    s = PnLSeries(address="0x", total_realized_pnl=200, span_days=10,
                   days=list("abcdef"),
                   steadiness=0.4, r_squared=0.5)
    assert _verdict(s) == "noisy positive"


def test_verdict_insufficient_history_when_few_buckets():
    s = PnLSeries(address="0x", total_realized_pnl=200, span_days=10,
                   days=["only", "three"],
                   steadiness=0.6, r_squared=0.9)
    assert _verdict(s) == "insufficient history"
