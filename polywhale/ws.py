"""Polymarket WebSocket client — live trade feed.

  wss://ws-live-data.polymarket.com   public; subscribe to topic 'activity'

The server expects a literal PING text frame every 5s. The subscribe
message is one JSON frame; trades come back as JSON frames with a
'topic' and 'type'. We filter to topic='activity', type='trades'.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Iterable, Optional, Set

import websockets

log = logging.getLogger(__name__)

WSS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5.0


async def stream_trades(
    watchlist: Optional[Iterable[str]] = None,
    *,
    url: str = WSS_URL,
) -> AsyncIterator[dict]:
    """Yield trade events from the public activity feed.

    If watchlist is given, only events whose proxyWallet is in the set are
    yielded. Comparison is lowercased.
    """
    wl: Optional[Set[str]] = (
        {a.lower() for a in watchlist} if watchlist is not None else None
    )

    sub_msg = json.dumps(
        {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
    )

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, max_size=2**22) as ws:
                await ws.send(sub_msg)
                log.info("ws subscribed: activity/trades watchlist=%s",
                         "all" if wl is None else len(wl))
                backoff = 1.0
                ping_task = asyncio.create_task(_pinger(ws))
                try:
                    async for raw in ws:
                        ev = _parse(raw)
                        if ev is None:
                            continue
                        if wl is not None:
                            pw = (ev.get("proxyWallet") or "").lower()
                            if pw not in wl:
                                continue
                        yield ev
                finally:
                    ping_task.cancel()
        except (websockets.WebSocketException, OSError) as e:
            log.warning("ws disconnect: %s — reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def _pinger(ws) -> None:
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await ws.send("PING")
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.debug("pinger exited: %s", e)


def _parse(raw) -> Optional[dict]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "ignore")
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or s in ("PONG", "PING"):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    # Server occasionally wraps payloads under 'payload' or sends bare records.
    if isinstance(obj, dict) and obj.get("topic") == "activity" and obj.get("type") == "trades":
        return obj.get("payload") or obj.get("data") or obj
    if isinstance(obj, dict) and "proxyWallet" in obj and "price" in obj:
        return obj
    return None
