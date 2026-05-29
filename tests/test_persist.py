import json
import os

from polywhale.persist import data_dir, load_latest, save_watchlist


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYWHALE_DATA_DIR", str(tmp_path))
    p = save_watchlist("BTC", [{"proxy_wallet": "0xabc", "pnl": {}}])
    assert p.exists()
    assert (tmp_path / "latest_btc.json").exists()
    loaded = load_latest("BTC")
    assert loaded is not None
    assert loaded["asset"] == "BTC"
    assert loaded["wallets"][0]["proxy_wallet"] == "0xabc"


def test_load_latest_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYWHALE_DATA_DIR", str(tmp_path))
    assert load_latest("DOGE") is None
