"""Whale discovery — find the biggest holders across a set of live markets.

Strategy:
  1. Search gamma /markets for active markets matching an asset keyword.
  2. For each market, pull /holders (capped at top N per outcome).
  3. Estimate USDC exposure per holder via amount * current outcome price.
  4. Aggregate across markets, dedupe by proxyWallet, rank by total exposure.

This is intentionally simple. It optimizes for finding *concentrated* whales
on a single asset (BTC), not the largest accounts overall.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from polywhale.api import PolymarketPublicClient

log = logging.getLogger(__name__)

# Polymarket's "Crypto" tag covers every per-coin and price-action market.
# We always filter at the gamma layer with this tag, then client-side filter
# by asset-specific keywords. Gamma's `question_ilike` parameter is silently
# ignored as of May 2026 — tag_id is the only reliable server-side filter.
CRYPTO_TAG_ID = 21

ASSET_KEYWORDS = {
    "BTC":  ("bitcoin", "btc"),
    "ETH":  ("ethereum", " eth ", " eth-"),
    "SOL":  ("solana", " sol "),
    "XRP":  ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
}


def _question_matches_asset(question: str, asset: str) -> bool:
    q = " " + (question or "").lower() + " "
    needles = ASSET_KEYWORDS.get(asset.upper(), (asset.lower(),))
    return any(n in q for n in needles)


@dataclass
class HolderHit:
    proxy_wallet: str
    pseudonym: Optional[str]
    name: Optional[str]
    verified: bool
    amount: float
    outcome_index: int
    condition_id: str
    market_question: str
    market_slug: str
    outcome_price: float  # last-known price of the held outcome
    exposure_usd: float   # amount * outcome_price


@dataclass
class WhaleRollup:
    proxy_wallet: str
    pseudonym: Optional[str] = None
    name: Optional[str] = None
    verified: bool = False
    total_exposure_usd: float = 0.0
    markets: List[HolderHit] = field(default_factory=list)

    def add(self, hit: HolderHit) -> None:
        self.pseudonym = self.pseudonym or hit.pseudonym
        self.name = self.name or hit.name
        self.verified = self.verified or hit.verified
        self.total_exposure_usd += hit.exposure_usd
        self.markets.append(hit)


def discover_whales(
    client: PolymarketPublicClient,
    *,
    asset: str = "BTC",
    market_limit: int = 30,
    holders_per_market: int = 50,
    min_exposure_usd: float = 100.0,
) -> List[WhaleRollup]:
    """Return whales ranked by total USD exposure across active markets for an asset."""
    markets: List[Dict] = []
    seen_ids: set = set()

    # Page through the crypto tag until we've collected `market_limit` markets
    # matching the asset keyword. Gamma returns up to 100 per page.
    page_size = 100
    offset = 0
    max_pages = 10
    for _ in range(max_pages):
        rows = client.search_markets(
            tag_id=CRYPTO_TAG_ID,
            active=True, closed=False,
            order="volume24hr", ascending=False,
            limit=page_size, offset=offset,
        )
        if not rows:
            break
        for raw in rows:
            m = client.parse_gamma_market(raw)
            if not m.get("enableOrderBook", True):
                continue
            cid = m.get("conditionId")
            if not cid or cid in seen_ids:
                continue
            if not _question_matches_asset(m.get("question", ""), asset):
                continue
            seen_ids.add(cid)
            markets.append(m)
            if len(markets) >= market_limit:
                break
        if len(markets) >= market_limit or len(rows) < page_size:
            break
        offset += page_size

    log.info("discover: %d unique active markets for %s", len(markets), asset)

    rollups: Dict[str, WhaleRollup] = defaultdict(lambda: WhaleRollup(proxy_wallet=""))

    for m in markets:
        cid = m["conditionId"]
        outcomes = m.get("outcomes") or []
        prices = [_to_float(p, 0.5) for p in (m.get("outcomePrices") or [])]
        try:
            holders_resp = client.holders(cid, limit=holders_per_market)
        except Exception as e:
            log.warning("holders %s failed: %s", cid, e)
            continue

        for token_block in holders_resp or []:
            for h in token_block.get("holders") or []:
                wallet = (h.get("proxyWallet") or "").lower()
                if not wallet:
                    continue
                amount = _to_float(h.get("amount"), 0.0)
                if amount <= 0:
                    continue
                oi = int(h.get("outcomeIndex") or 0)
                price = prices[oi] if oi < len(prices) else 0.5
                exposure = amount * price
                if exposure < 1.0:
                    continue
                hit = HolderHit(
                    proxy_wallet=wallet,
                    pseudonym=h.get("pseudonym"),
                    name=h.get("name"),
                    verified=bool(h.get("verified")),
                    amount=amount,
                    outcome_index=oi,
                    condition_id=cid,
                    market_question=m.get("question", ""),
                    market_slug=m.get("slug", ""),
                    outcome_price=price,
                    exposure_usd=exposure,
                )
                ru = rollups[wallet]
                if not ru.proxy_wallet:
                    ru.proxy_wallet = wallet
                ru.add(hit)

    out = [r for r in rollups.values() if r.total_exposure_usd >= min_exposure_usd]
    out.sort(key=lambda r: r.total_exposure_usd, reverse=True)
    return out


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
