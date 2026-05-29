"""polywhale CLI — `polywhale discover|analyze|resolve|macro|watch`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from typing import Iterable

from polywhale import __version__
from polywhale.analyze import fingerprint
from polywhale.api import PolymarketPublicClient
from polywhale.discover import discover_whales
from polywhale.macro import macro_snapshot
from polywhale.watch import watch_wallets


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="polywhale",
        description="Discover and analyze algorithmic whales on Polymarket.",
    )
    parser.add_argument("--version", action="version", version=f"polywhale {__version__}")
    parser.add_argument("--log-level", default=os.environ.get("POLYWHALE_LOG_LEVEL", "INFO"))

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_disc = sub.add_parser("discover", help="Find whales across active markets for an asset.")
    p_disc.add_argument("--asset", default="BTC")
    p_disc.add_argument("--market-limit", type=int, default=30)
    p_disc.add_argument("--holders", type=int, default=50)
    p_disc.add_argument("--min-exposure", type=float, default=500.0)
    p_disc.add_argument("--top", type=int, default=20)
    p_disc.add_argument("--json", action="store_true")
    p_disc.set_defaults(func=_cmd_discover)

    p_an = sub.add_parser("analyze", help="Fingerprint one wallet (address or handle).")
    p_an.add_argument("target", help="0x address, @handle, or polymarket.com profile URL")
    p_an.add_argument("--trades", type=int, default=500)
    p_an.add_argument("--json", action="store_true")
    p_an.set_defaults(func=_cmd_analyze)

    p_rs = sub.add_parser("resolve", help="Resolve a handle or profile URL to an address.")
    p_rs.add_argument("target")
    p_rs.set_defaults(func=_cmd_resolve)

    p_mc = sub.add_parser("macro", help="Snapshot active markets for an asset.")
    p_mc.add_argument("--asset", default="BTC")
    p_mc.add_argument("--limit", type=int, default=25)
    p_mc.add_argument("--midpoint", action="store_true",
                       help="Also fetch CLOB midpoint per market (slower).")
    p_mc.add_argument("--json", action="store_true")
    p_mc.set_defaults(func=_cmd_macro)

    p_wt = sub.add_parser("watch", help="Stream CopySignal events from watched wallets.")
    p_wt.add_argument("wallets", nargs="*",
                       help="Wallet addresses to follow. Defaults to $POLYWHALE_WATCHLIST.")
    p_wt.add_argument("--resolve", action="store_true",
                       help="Resolve each argument as a handle first.")
    p_wt.set_defaults(func=_cmd_watch)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    return args.func(args)


# ----------------------------------------------------------------
# command handlers
# ----------------------------------------------------------------
def _cmd_discover(args) -> int:
    c = PolymarketPublicClient()
    whales = discover_whales(
        c,
        asset=args.asset,
        market_limit=args.market_limit,
        holders_per_market=args.holders,
        min_exposure_usd=args.min_exposure,
    )[: args.top]

    if args.json:
        out = [{
            "proxy_wallet": w.proxy_wallet,
            "pseudonym": w.pseudonym,
            "name": w.name,
            "verified": w.verified,
            "total_exposure_usd": round(w.total_exposure_usd, 2),
            "n_markets": len(w.markets),
            "markets": [asdict(h) for h in w.markets],
        } for w in whales]
        print(json.dumps(out, indent=2))
        return 0

    print(f"\nTop {len(whales)} whales on {args.asset} markets by USD exposure:\n")
    print(f"{'#':>3}  {'wallet':42}  {'pseudonym':24}  {'exposure':>13}  {'mkts':>4}")
    print("-" * 96)
    for i, w in enumerate(whales, 1):
        pseu = (w.pseudonym or "—")[:24]
        print(f"{i:>3}  {w.proxy_wallet:42}  {pseu:24}  "
              f"${w.total_exposure_usd:>11,.0f}  {len(w.markets):>4}")
    return 0


def _cmd_analyze(args) -> int:
    c = PolymarketPublicClient()
    addr = c.resolve_handle(args.target)
    fp = fingerprint(c, addr, n_trades=args.trades)
    if args.json:
        print(json.dumps(asdict(fp), indent=2))
    else:
        print(fp.summary())
        if fp.top_markets:
            print("\nMost-traded markets:")
            for m in fp.top_markets:
                print(f"  - {m['title'][:70]:70}  trades={m['trades']:>4}  "
                      f"vol=${m['usd_volume']:>10,.2f}")
    return 0


def _cmd_resolve(args) -> int:
    c = PolymarketPublicClient()
    print(c.resolve_handle(args.target))
    return 0


def _cmd_macro(args) -> int:
    c = PolymarketPublicClient()
    snaps = macro_snapshot(c, asset=args.asset, limit=args.limit,
                            include_midpoint=args.midpoint)
    if args.json:
        print(json.dumps([asdict(s) for s in snaps], indent=2))
        return 0
    print(f"\n{args.asset} markets, top {len(snaps)} by 24h volume:\n")
    print(f"{'24h vol':>12}  {'liq':>10}  {'mins-left':>9}  {'skew':>5}  question")
    print("-" * 110)
    for s in snaps:
        skew = s.yes_no_skew()
        skew_str = f"{skew:.2f}" if skew is not None else " —"
        mins = f"{s.minutes_to_close:.0f}" if s.minutes_to_close is not None else "—"
        print(f"${s.volume_24h:>11,.0f}  ${s.liquidity:>9,.0f}  {mins:>9}  {skew_str:>5}  "
              f"{s.question[:70]}")
    return 0


def _cmd_watch(args) -> int:
    wallets: list[str] = list(args.wallets)
    if not wallets:
        env = os.environ.get("POLYWHALE_WATCHLIST", "").strip()
        if env:
            wallets = [w.strip() for w in env.split(",") if w.strip()]
    if not wallets:
        print("error: no wallets to watch (positional args or $POLYWHALE_WATCHLIST)",
              file=sys.stderr)
        return 2

    if args.resolve:
        c = PolymarketPublicClient()
        wallets = [c.resolve_handle(w) for w in wallets]

    return asyncio.run(_run_watch(wallets))


async def _run_watch(wallets: Iterable[str]) -> int:
    try:
        async for sig in watch_wallets(wallets):
            print(json.dumps(sig.to_dict(), separators=(",", ":")), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
