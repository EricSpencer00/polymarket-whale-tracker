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
from polywhale.autopilot import QualifyParams, qualify_whales, persist as persist_watchlist, style_hint_map
from polywhale.discover import discover_whales
from polywhale.macro import macro_snapshot
from polywhale.pnl import reconstruct as reconstruct_pnl
from polywhale.scan import scan_fast_markets, top_traders
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

    p_sf = sub.add_parser("scan-fast",
                            help="Rank wallets by USDC volume in recent fast markets.")
    p_sf.add_argument("--asset", default="BTC")
    p_sf.add_argument("--hours", type=float, default=6.0,
                       help="Look-back window in hours.")
    p_sf.add_argument("--markets", type=int, default=80,
                       help="Maximum number of distinct markets to scan.")
    p_sf.add_argument("--min-volume", type=float, default=1000.0)
    p_sf.add_argument("--top", type=int, default=25)
    p_sf.add_argument("--json", action="store_true")
    p_sf.set_defaults(func=_cmd_scan_fast)

    p_pnl = sub.add_parser("pnl", help="Reconstruct daily PnL trajectory for a wallet.")
    p_pnl.add_argument("target", help="0x address, @handle, or polymarket.com URL")
    p_pnl.add_argument("--days", type=int, default=30)
    p_pnl.add_argument("--max-events", type=int, default=10000)
    p_pnl.add_argument("--json", action="store_true")
    p_pnl.set_defaults(func=_cmd_pnl)

    p_q = sub.add_parser("qualify",
                          help="scan-fast -> PnL filter -> ranked watchlist of steady winners.")
    p_q.add_argument("--asset", default="BTC")
    p_q.add_argument("--hours", type=float, default=6.0)
    p_q.add_argument("--markets", type=int, default=80)
    p_q.add_argument("--candidates", type=int, default=25,
                      help="Top-N candidates by volume to evaluate PnL for.")
    p_q.add_argument("--days", type=int, default=14,
                      help="PnL trajectory window in days.")
    p_q.add_argument("--min-volume", type=float, default=1000.0)
    p_q.add_argument("--min-pnl", type=float, default=0.0)
    p_q.add_argument("--min-steadiness", type=float, default=0.35)
    p_q.add_argument("--json", action="store_true")
    p_q.set_defaults(func=_cmd_qualify)

    p_ap = sub.add_parser(
        "autopilot",
        help="Find conviction+edge BTC whales and live-stream their signals.",
    )
    p_ap.add_argument("--asset", default="BTC")
    p_ap.add_argument("--hours", type=float, default=6.0,
                       help="Scan-fast look-back in hours.")
    p_ap.add_argument("--markets", type=int, default=80)
    p_ap.add_argument("--candidates", type=int, default=30)
    p_ap.add_argument("--days", type=int, default=7,
                       help="PnL trajectory window in days.")
    p_ap.add_argument("--min-volume", type=float, default=1500.0)
    p_ap.add_argument("--min-pnl", type=float, default=0.0)
    p_ap.add_argument("--min-steadiness", type=float, default=0.45)
    p_ap.add_argument("--min-conviction", type=float, default=0.20)
    p_ap.add_argument("--min-clip", type=float, default=0.0,
                       help="Drop wallets whose median clip is below this USDC value.")
    p_ap.add_argument("--max-idle-minutes", type=float, default=240.0,
                       help="Drop wallets that haven't traded in the last N minutes.")
    p_ap.add_argument("--top", type=int, default=10,
                       help="Cap the watchlist to the best N wallets.")
    p_ap.add_argument("--no-watch", action="store_true",
                       help="Stop after qualification + persist; don't open the WSS stream.")
    p_ap.add_argument("--no-persist", action="store_true",
                       help="Skip writing the watchlist to data/.")
    p_ap.add_argument("--json", action="store_true",
                       help="Emit the qualified watchlist as JSON instead of a table.")
    p_ap.set_defaults(func=_cmd_autopilot)

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


def _cmd_scan_fast(args) -> int:
    c = PolymarketPublicClient()
    tallies = scan_fast_markets(
        c, asset=args.asset, window_hours=args.hours,
        market_cap=args.markets,
    )
    ranked = top_traders(tallies, min_volume_usd=args.min_volume, limit=args.top)

    if args.json:
        out = [{
            "proxy_wallet": t.proxy_wallet,
            "pseudonym": t.pseudonym,
            "usdc_volume": round(t.usdc_volume, 2),
            "trade_count": t.trade_count,
            "buy_share": round(t.buy_share, 3),
            "distinct_markets": t.distinct_markets,
            "last_seen": t.last_seen,
        } for t in ranked]
        print(json.dumps(out, indent=2))
        return 0

    print(f"\nTop {len(ranked)} wallets by USDC volume in fast {args.asset} markets "
          f"(last {args.hours:.1f}h):\n")
    print(f"{'#':>3}  {'wallet':42}  {'pseudonym':22}  "
          f"{'volume':>11}  {'trades':>7}  {'buy%':>5}  {'mkts':>4}")
    print("-" * 108)
    for i, t in enumerate(ranked, 1):
        pseu = (t.pseudonym or "—")[:22]
        print(f"{i:>3}  {t.proxy_wallet:42}  {pseu:22}  "
              f"${t.usdc_volume:>9,.0f}  {t.trade_count:>7}  "
              f"{t.buy_share*100:>4.0f}%  {t.distinct_markets:>4}")
    return 0


def _cmd_pnl(args) -> int:
    c = PolymarketPublicClient()
    addr = c.resolve_handle(args.target)
    series = reconstruct_pnl(c, addr, days_back=args.days, max_events=args.max_events)
    if args.json:
        print(json.dumps({
            "address": series.address,
            "days": series.days,
            "daily_pnl": series.daily_pnl,
            "cumulative_pnl": series.cumulative_pnl,
            "metrics": {
                "n_events": series.n_events,
                "span_days": series.span_days,
                "total_realized_pnl": series.total_realized_pnl,
                "slope_usd_per_day": series.slope_usd_per_day,
                "r_squared": series.r_squared,
                "max_drawdown": series.max_drawdown,
                "max_drawdown_ratio": series.max_drawdown_ratio,
                "winning_day_rate": series.winning_day_rate,
                "steadiness": series.steadiness,
                "verdict": series.verdict,
            },
        }, indent=2))
        return 0
    print(series.summary())
    if series.days:
        print("\nLast 10 days (USD):")
        for d, dp, cp in list(zip(series.days, series.daily_pnl, series.cumulative_pnl))[-10:]:
            arrow = "+" if dp >= 0 else "-"
            print(f"  {d}   day {arrow}${abs(dp):>10,.2f}   cum ${cp:>11,.2f}")
    return 0


def _cmd_qualify(args) -> int:
    c = PolymarketPublicClient()
    print(f"[1/2] scanning fast {args.asset} markets...", file=sys.stderr)
    tallies = scan_fast_markets(
        c, asset=args.asset, window_hours=args.hours, market_cap=args.markets,
    )
    candidates = top_traders(
        tallies, min_volume_usd=args.min_volume, limit=args.candidates,
    )
    print(f"[2/2] qualifying {len(candidates)} candidates by PnL trajectory "
          f"(window {args.days}d)...", file=sys.stderr)

    qualified = []
    for t in candidates:
        try:
            series = reconstruct_pnl(c, t.proxy_wallet, days_back=args.days)
        except Exception as e:
            print(f"  pnl({t.proxy_wallet}) failed: {e}", file=sys.stderr)
            continue
        if (series.total_realized_pnl < args.min_pnl or
                series.steadiness < args.min_steadiness):
            continue
        qualified.append((t, series))

    qualified.sort(key=lambda ts: ts[1].steadiness, reverse=True)

    if args.json:
        out = [{
            "proxy_wallet": t.proxy_wallet,
            "pseudonym": t.pseudonym,
            "scan": {
                "usdc_volume": round(t.usdc_volume, 2),
                "trade_count": t.trade_count,
                "buy_share": round(t.buy_share, 3),
                "distinct_markets": t.distinct_markets,
            },
            "pnl": {
                "realized_pnl": round(s.total_realized_pnl, 2),
                "slope_usd_per_day": round(s.slope_usd_per_day, 2),
                "r_squared": round(s.r_squared, 3),
                "max_drawdown": round(s.max_drawdown, 2),
                "max_drawdown_ratio": round(s.max_drawdown_ratio, 3),
                "winning_day_rate": round(s.winning_day_rate, 3),
                "steadiness": round(s.steadiness, 3),
                "verdict": s.verdict,
                "span_days": s.span_days,
            },
        } for t, s in qualified]
        print(json.dumps(out, indent=2))
        return 0

    print(f"\nQualified {args.asset} whales (volume>=${args.min_volume:.0f}, "
          f"steady positive PnL):\n")
    print(f"{'#':>3}  {'wallet':42}  {'pseudonym':18}  "
          f"{'scan vol':>10}  {'PnL':>10}  {'$/day':>8}  {'R^2':>5}  {'steady':>6}  verdict")
    print("-" * 130)
    for i, (t, s) in enumerate(qualified, 1):
        pseu = (t.pseudonym or "—")[:18]
        print(f"{i:>3}  {t.proxy_wallet:42}  {pseu:18}  "
              f"${t.usdc_volume:>8,.0f}  ${s.total_realized_pnl:>8,.0f}  "
              f"${s.slope_usd_per_day:>6,.1f}  {s.r_squared:>5.2f}  "
              f"{s.steadiness:>6.2f}  {s.verdict}")
    return 0


def _cmd_autopilot(args) -> int:
    c = PolymarketPublicClient()
    params = QualifyParams(
        asset=args.asset, hours=args.hours, markets=args.markets,
        candidates=args.candidates, days=args.days,
        min_volume_usd=args.min_volume, min_pnl_usd=args.min_pnl,
        min_steadiness=args.min_steadiness, min_conviction=args.min_conviction,
        min_median_clip_usd=args.min_clip,
        max_minutes_since_trade=args.max_idle_minutes,
    )
    print(f"[1/3] scanning fast {args.asset} markets (last {args.hours:.1f}h)...",
          file=sys.stderr)
    qualified = qualify_whales(c, params)[: args.top]

    if not args.no_persist:
        path = persist_watchlist(args.asset, qualified, params)
        print(f"[2/3] watchlist saved -> {path}", file=sys.stderr)
    else:
        print(f"[2/3] persistence skipped (--no-persist)", file=sys.stderr)

    if args.json:
        print(json.dumps([q.to_dict() for q in qualified], indent=2))
    else:
        _print_qualified_table(args.asset, qualified)

    if args.no_watch or not qualified:
        if not qualified:
            print("\nNo wallets passed all qualification gates.", file=sys.stderr)
        return 0

    wallets = [q.proxy_wallet for q in qualified]
    hints = style_hint_map(qualified)
    print(f"\n[3/3] streaming live signals from {len(wallets)} wallets "
          f"(Ctrl-C to stop)...", file=sys.stderr)
    return asyncio.run(_run_watch_with_hints(wallets, hints))


def _print_qualified_table(asset: str, qualified) -> None:
    print(f"\nQualified {asset} whales (conviction + edge):\n")
    print(
        f"{'#':>3}  {'wallet':42}  {'pseudonym':20}  "
        f"{'PnL':>9}  {'$/day':>9}  {'R^2':>4}  "
        f"{'steady':>6}  {'conv':>5}  {'medClip':>8}  {'idle':>5}  style"
    )
    print("-" * 135)
    for i, q in enumerate(qualified, 1):
        pseu = (q.pseudonym or "—")[:20]
        print(
            f"{i:>3}  {q.proxy_wallet:42}  {pseu:20}  "
            f"${q.pnl.total_realized_pnl:>7,.0f}  "
            f"${q.pnl.slope_usd_per_day:>7,.0f}  "
            f"{q.pnl.r_squared:>4.2f}  "
            f"{q.pnl.steadiness:>6.2f}  "
            f"{q.conviction.conviction_score:>5.2f}  "
            f"${q.conviction.median_clip_usd:>6,.1f}  "
            f"{q.minutes_since_last_trade:>4.0f}m  "
            f"{q.style_hint()}"
        )


async def _run_watch_with_hints(wallets, hints):
    try:
        async for sig in watch_wallets(wallets, style_hints=hints):
            print(json.dumps(sig.to_dict(), separators=(",", ":")), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
