from polywhale.fast import detect, is_fast, asset_symbol


def test_detect_btc_5m():
    fm = detect("Bitcoin Up or Down - May 28, 9:25PM-9:30PM ET")
    assert fm is not None
    assert fm.asset == "Bitcoin"
    assert fm.window_minutes == 5


def test_detect_btc_15m_crossing_pm():
    fm = detect("Bitcoin Up or Down - May 28, 9:15PM-9:30PM ET")
    assert fm is not None
    assert fm.window_minutes == 15


def test_detect_eth_5m():
    fm = detect("Ethereum Up or Down - May 28, 11:55AM-12:00PM ET")
    assert fm is not None
    assert fm.asset == "Ethereum"
    assert fm.window_minutes == 5


def test_detect_rejects_slow_market():
    assert detect("Will Bitcoin reach $150,000 by June 30, 2026?") is None
    assert detect("MicroStrategy sells any Bitcoin by May 31, 2026?") is None


def test_detect_rejects_unsupported_window():
    # 7 minutes is not in ALLOWED_WINDOWS
    assert detect("Bitcoin Up or Down - May 28, 9:00PM-9:07PM ET") is None


def test_detect_rejects_other_asset():
    assert detect("Cardano Up or Down - May 28, 9:25PM-9:30PM ET") is None


def test_is_fast_helper():
    assert is_fast("Bitcoin Up or Down - May 28, 9:25PM-9:30PM ET")
    assert not is_fast("Will Bitcoin reach $150,000 by June 30?")


def test_asset_symbol_mapping():
    assert asset_symbol("Bitcoin") == "BTC"
    assert asset_symbol("Ethereum") == "ETH"
    assert asset_symbol("Solana") == "SOL"
    assert asset_symbol("XRP") == "XRP"
    assert asset_symbol("Dogecoin") == "DOGE"
