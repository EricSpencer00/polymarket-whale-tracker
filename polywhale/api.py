"""Polymarket public REST client — read-only.

Three hosts, no auth required:
  data-api.polymarket.com   user analytics: positions, activity, trades, holders, value
  gamma-api.polymarket.com  market metadata, public profiles, events
  clob.polymarket.com       order book, midpoint, best bid/ask

Rate limits (per IP):
  gamma-api  4,000 req / 10s
  data-api   1,000 req / 10s
  clob       ~50  req / 10s   (higher with an API key, which we don't need)

Cloudflare queues over-limit requests rather than 429-ing them, so a single
process pulling sequentially basically never hits the wall.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Polymarket renders user URLs in a few shapes:
#   polymarket.com/@<address>-<numeric>?tab=...
#   polymarket.com/@<username>
#   polymarket.com/profile/<address>
_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")


class PolyAPIError(RuntimeError):
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


class PolymarketPublicClient:
    """Thin wrapper over Polymarket's three public REST hosts."""

    def __init__(self, timeout: float = 30.0, user_agent: str = "polywhale/0.1"):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.s.headers["Accept"] = "application/json"

    # ----------------------------------------------------------------
    # low-level
    # ----------------------------------------------------------------
    def _get(self, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = base + path
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)}"
        r = self.s.get(url, timeout=self.timeout)
        if not r.ok:
            raise PolyAPIError(
                f"GET {url} -> {r.status_code}: {r.text[:300]}",
                status=r.status_code, body=r.text,
            )
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # ----------------------------------------------------------------
    # data-api: user-centric
    # ----------------------------------------------------------------
    def positions(
        self,
        user: str,
        *,
        limit: int = 500,
        offset: int = 0,
        size_threshold: float = 0,
        sort_by: str = "CURRENT",
        sort_direction: str = "DESC",
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params = {
            "user": user,
            "limit": limit,
            "offset": offset,
            "sizeThreshold": size_threshold,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
            "market": market,
        }
        return self._get(DATA_API, "/positions", params) or []

    def all_positions(self, user: str, *, size_threshold: float = 0,
                      page_size: int = 500) -> Iterator[Dict[str, Any]]:
        offset = 0
        while True:
            page = self.positions(user, limit=page_size, offset=offset,
                                   size_threshold=size_threshold)
            if not page:
                return
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    def trades(
        self,
        user: str,
        *,
        limit: int = 500,
        offset: int = 0,
        taker_only: bool = False,
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params = {
            "user": user,
            "limit": limit,
            "offset": offset,
            "takerOnly": "true" if taker_only else None,
            "market": market,
        }
        return self._get(DATA_API, "/trades", params) or []

    def all_trades(self, user: str, *, page_size: int = 500,
                    max_pages: int = 200) -> Iterator[Dict[str, Any]]:
        offset = 0
        for _ in range(max_pages):
            page = self.trades(user, limit=page_size, offset=offset)
            if not page:
                return
            yield from page
            if len(page) < page_size:
                return
            offset += page_size

    def activity(
        self,
        user: str,
        *,
        limit: int = 500,
        offset: int = 0,
        types: Iterable[str] = ("TRADE",),
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params = {
            "user": user,
            "limit": limit,
            "offset": offset,
            "type": ",".join(types) if types else None,
            "start": start,
            "end": end,
        }
        return self._get(DATA_API, "/activity", params) or []

    def portfolio_value(self, user: str) -> float:
        """Total USD value of a wallet's open positions."""
        rows = self._get(DATA_API, "/value", {"user": user}) or []
        if not rows:
            return 0.0
        return float(rows[0].get("value", 0.0))

    def holders(self, condition_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Top holders for a market (one entry per outcome token).

        Returns: [{token, holders: [{proxyWallet, pseudonym, name, amount,
                  outcomeIndex, verified}]}, ...]
        """
        return self._get(DATA_API, "/holders", {"market": condition_id, "limit": limit}) or []

    # ----------------------------------------------------------------
    # gamma-api: market metadata + profiles
    # ----------------------------------------------------------------
    def public_profile(self, address: str) -> Dict[str, Any]:
        return self._get(GAMMA_API, "/public-profile", {"address": address}) or {}

    def search_markets(
        self,
        *,
        question_ilike: Optional[str] = None,
        tag_slug: Optional[str] = None,
        tag_id: Optional[int] = None,
        active: Optional[bool] = True,
        closed: Optional[bool] = False,
        order: str = "volume24hr",
        ascending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        params = {
            "active": _bool_param(active),
            "closed": _bool_param(closed),
            "question_ilike": question_ilike,
            "tag_slug": tag_slug,
            "tag_id": tag_id,
            "order": order,
            "ascending": "true" if ascending else "false",
            "limit": limit,
            "offset": offset,
        }
        return self._get(GAMMA_API, "/markets", params) or []

    def event(self, slug_or_id: str) -> Dict[str, Any]:
        return self._get(GAMMA_API, f"/events/{slug_or_id}") or {}

    # ----------------------------------------------------------------
    # clob: order book
    # ----------------------------------------------------------------
    def book(self, token_id: str) -> Dict[str, Any]:
        return self._get(CLOB_API, "/book", {"token_id": token_id}) or {}

    def midpoint(self, token_id: str) -> Optional[float]:
        r = self._get(CLOB_API, "/midpoint", {"token_id": token_id})
        if not r:
            return None
        try:
            return float(r.get("mid"))
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------------
    # helpers
    # ----------------------------------------------------------------
    def resolve_handle(self, handle_or_address: str) -> str:
        """Resolve a Polymarket username, profile URL, or 0x address to an address.

        Polymarket has no public username->address API. For handles we scrape
        the public profile page once and pull `proxyWallet` from the embedded
        JSON. Addresses pass through.
        """
        s = handle_or_address.strip()
        m = _ADDR_RE.search(s)
        if m:
            return m.group(0).lower()
        # Handle form: @boneweeper or boneweeper or full polymarket.com/@... URL
        s = s.lstrip("@")
        if "polymarket.com/" not in s:
            url = f"https://polymarket.com/@{s}"
        else:
            url = "https://" + s if not s.startswith("http") else s
        log.debug("scraping %s for proxyWallet", url)
        r = self.s.get(url, timeout=self.timeout, headers={"User-Agent": "polywhale/0.1"})
        if not r.ok:
            raise PolyAPIError(f"resolve_handle: {url} -> {r.status_code}", status=r.status_code)
        m = re.search(r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"', r.text)
        if not m:
            raise PolyAPIError(f"no proxyWallet in profile page for {handle_or_address!r}")
        return m.group(1).lower()

    @staticmethod
    def parse_gamma_market(m: Dict[str, Any]) -> Dict[str, Any]:
        """Gamma encodes some fields as JSON-in-JSON. Decode them here.

        Returns a shallow copy with outcomes / outcomePrices / clobTokenIds
        decoded into real lists.
        """
        out = dict(m)
        for key in ("outcomes", "outcomePrices", "clobTokenIds"):
            v = out.get(key)
            if isinstance(v, str):
                try:
                    out[key] = json.loads(v)
                except ValueError:
                    pass
        return out


def now_ms() -> int:
    return int(time.time() * 1000)


def _bool_param(v: Optional[bool]) -> Optional[str]:
    """Serialize a tri-state bool for gamma query strings.

    None -> dropped from URL (no filter).
    True/False -> "true"/"false".
    """
    if v is None:
        return None
    return "true" if v else "false"
