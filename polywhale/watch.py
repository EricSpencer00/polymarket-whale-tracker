"""Live watcher — open one WSS, filter by watchlist, emit CopySignal events.

This is the consumer-facing API for downstream executors. You give it a set
of wallet addresses, an async callback (or use it as an async iterator),
and it streams structured signals as the watched wallets trade.

Usage:
    async for sig in watch_wallets({"0xb17a..."}):
        print(sig.market_slug, sig.side, sig.size, sig.price)
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, Iterable, Optional

from polywhale.signal import CopySignal
from polywhale.ws import stream_trades

log = logging.getLogger(__name__)


async def watch_wallets(
    watchlist: Iterable[str],
    *,
    style_hints: Optional[Dict[str, str]] = None,
) -> AsyncIterator[CopySignal]:
    """Yield CopySignal events for trades from any of the watchlisted wallets.

    `style_hints` is an optional {wallet -> style label} map produced by the
    analyzer; it tags each signal so a consumer can route 'market_maker'
    vs 'hf_scalper_straddle' wallets to different handlers.
    """
    wl = {a.lower() for a in watchlist}
    hints = {(k or "").lower(): v for k, v in (style_hints or {}).items()}
    async for ev in stream_trades(watchlist=wl):
        sig = _to_signal(ev, hints)
        if sig is not None:
            yield sig


def _to_signal(ev: dict, hints: Dict[str, str]) -> Optional[CopySignal]:
    wallet = (ev.get("proxyWallet") or "").lower()
    if not wallet:
        return None
    try:
        price = float(ev.get("price") or 0.0)
        size = float(ev.get("size") or 0.0)
    except (TypeError, ValueError):
        return None
    usd = float(ev.get("usdcSize") or (price * size))
    return CopySignal(
        source_wallet=wallet,
        source_pseudonym=ev.get("pseudonym"),
        market_slug=ev.get("slug", ""),
        market_question=ev.get("title", ""),
        condition_id=ev.get("conditionId", ""),
        asset_token_id=str(ev.get("asset", "")),
        outcome=ev.get("outcome", ""),
        side=str(ev.get("side", "")).upper(),
        price=price,
        size=size,
        usdc_size=usd,
        timestamp=int(ev.get("timestamp") or 0),
        transaction_hash=ev.get("transactionHash"),
        style_hint=hints.get(wallet),
        extras={k: ev[k] for k in ("outcomeIndex", "eventSlug", "name") if k in ev},
    )
