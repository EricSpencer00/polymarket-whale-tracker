"""
Edge scanner — runs every 8h via GitHub Actions.

Fetches top 50 Polymarket earners, reconstructs true PnL (including losses
that the Polymarket UI hides), extracts cross-wallet patterns, and writes
findings to data/edge_scan_YYYYMMDD_HHMMSS.json.

Exits with code 2 if edge decay >30% vs previous scan (used by CI to
open a GitHub issue).

True PnL formula (Polymarket hides losses — positions expire at $0):
    win_pnl  = (1 - entry_price) × contracts - fee
    loss_pnl = -entry_price × contracts - fee
    fee      = 0.07 × min(entry_price, 1 - entry_price) × contracts
    true_WR  = wins / (wins + losses)
    true_EV  = mean(win_pnl) × WR + mean(loss_pnl) × (1-WR)
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode
import urllib.request

# -- path setup so polywhale package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from polywhale.api import PolymarketPublicClient

LB_API = "https://lb-api.polymarket.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "findings")
MIN_TRADES = 50
MIN_TRUE_PROFIT_USD = 200
TOP_N = 50
MAX_PAGES_PER_WALLET = 40
EV_DECAY_THRESHOLD = 0.30
MIN_EV_USD = 0.20
BUCKET_MIN_TRADES = 10


# ── True PnL ──────────────────────────────────────────────────────────────────

def pm_fee(price: float, contracts: float) -> float:
    return 0.07 * min(price, 1.0 - price) * contracts


def trade_pnl(entry: float, contracts: float, won: bool) -> float:
    fee = pm_fee(entry, contracts)
    if won:
        return (1.0 - entry) * contracts - fee
    return -entry * contracts - fee


# ── Leaderboard ───────────────────────────────────────────────────────────────

def fetch_top_wallets(n: int = TOP_N) -> List[Dict]:
    """Pull top earners from lb-api. Returns [{proxyWallet, amount, ...}]."""
    url = f"{LB_API}/profit?limit={n}"
    req = urllib.request.Request(url, headers={"User-Agent": "polywhale-edge-scan/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    # Filter: require minimum realized profit
    results = []
    for row in rows:
        raw = float(row.get("amount", 0) or 0)
        usd = raw / 1e6  # raw is in micro-USDC
        if usd >= MIN_TRUE_PROFIT_USD:
            results.append({
                "proxy_wallet": row.get("proxyWallet") or row.get("proxy_wallet", ""),
                "lb_profit_usd": round(usd, 2),
            })
    return results[:n]


# ── Activity scraper (cursor-based, avoids the offset-2000 cliff) ─────────────

def fetch_full_activity(client: PolymarketPublicClient, wallet: str) -> List[Dict]:
    """
    Fetch complete activity using end-timestamp cursor pagination.
    Fetches BUY and REDEEM events — the two types needed for true PnL.
    Stops after MAX_PAGES_PER_WALLET pages.
    """
    all_events = []
    end_cursor: Optional[int] = None
    for page in range(MAX_PAGES_PER_WALLET):
        batch = client.activity(
            wallet,
            limit=500,
            offset=0,           # always 0 — cursor does the pagination
            types=("TRADE",),   # TRADE includes both BUY and REDEEM sub-types
            end=end_cursor,
        )
        if not batch:
            break
        all_events.extend(batch)
        # Use the oldest timestamp as the next cursor
        timestamps = [int(e.get("timestamp", 0) or 0) for e in batch if e.get("timestamp")]
        if not timestamps or len(batch) < 500:
            break
        end_cursor = min(timestamps) - 1
        time.sleep(0.3)
    return all_events


# ── True PnL reconstruction ───────────────────────────────────────────────────

def reconstruct_trades(events: List[Dict]) -> List[Dict]:
    """
    Match BUY events to their outcomes (REDEEM = win, no redeem = loss).
    Returns list of completed trades with true PnL attached.

    Polymarket activity types:
      type=TRADE, side=BUY  → entry
      type=TRADE, side=SELL → early exit (rare)
      type=REDEEM           → winning position paid out

    Positions that expire worthless produce NO event — they simply don't
    appear as REDEEM. We detect losses by checking which BUYs have no
    matching REDEEM.
    """
    # Separate by type
    buys: Dict[str, List[Dict]] = defaultdict(list)    # asset_id → [buy events]
    redeems: Dict[str, List[Dict]] = defaultdict(list) # asset_id → [redeem events]

    for e in events:
        asset = e.get("asset") or e.get("token_id") or e.get("proxyWallet", "")
        market = e.get("conditionId") or e.get("market") or ""
        side = (e.get("side") or "").upper()
        typ = (e.get("type") or "").upper()

        # Use conditionId as the grouping key; fall back to asset
        key = market or asset

        if typ == "TRADE" and side == "BUY":
            price = float(e.get("price") or e.get("usdcSize", 0) or 0)
            size = float(e.get("size") or e.get("shares", 0) or 0)
            if price > 0 and size > 0:
                buys[key].append({
                    "key": key,
                    "timestamp": int(e.get("timestamp", 0) or 0),
                    "price": price,
                    "size": size,
                    "outcome": e.get("outcome") or e.get("side"),
                    "slug": e.get("slug") or e.get("market") or "",
                    "raw": e,
                })
        elif typ == "REDEEM" or (typ == "TRADE" and side == "REDEEM"):
            redeems[key].append(e)

    # Match: for each buy group, check if there's a redeem
    trades = []
    for key, buy_list in buys.items():
        has_redeem = key in redeems and len(redeems[key]) > 0
        for buy in buy_list:
            won = has_redeem
            pnl = trade_pnl(buy["price"], buy["size"], won)
            trades.append({
                "timestamp": buy["timestamp"],
                "entry_price": buy["price"],
                "contracts": buy["size"],
                "won": won,
                "pnl_usd": round(pnl, 4),
                "fee_usd": round(pm_fee(buy["price"], buy["size"]), 4),
                "slug": buy["slug"],
                "market_key": key,
            })

    return sorted(trades, key=lambda t: t["timestamp"])


# ── Pattern extraction ────────────────────────────────────────────────────────

def classify_market(slug: str) -> str:
    slug = slug.lower()
    if "5m" in slug or "5-min" in slug:
        return "5m"
    if "15m" in slug or "15-min" in slug:
        return "15m"
    if "1h" in slug or "1hr" in slug or "hourly" in slug:
        return "1h"
    if "daily" in slug or "24h" in slug or "1d" in slug:
        return "daily"
    if any(x in slug for x in ["nfl", "nba", "mlb", "nhl", "soccer", "sport"]):
        return "sports"
    if any(x in slug for x in ["election", "president", "congress", "senate"]):
        return "politics"
    return "other"


def price_bucket(p: float) -> str:
    if p < 0.50:
        return "<0.50"
    if p < 0.60:
        return "0.50-0.60"
    if p < 0.70:
        return "0.60-0.70"
    if p < 0.80:
        return "0.70-0.80"
    if p < 0.90:
        return "0.80-0.90"
    return "0.90+"


def extract_patterns(trades: List[Dict]) -> Dict[str, Dict]:
    """
    Bucket trades by (market_type, price_bucket).
    Return dict: bucket_label → {trade_count, true_wr, true_ev, wins, losses}
    """
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        mtype = classify_market(t.get("slug", ""))
        pbucket = price_bucket(t["entry_price"])
        label = f"{mtype}|{pbucket}"
        buckets[label].append(t)

    results = {}
    for label, ts in buckets.items():
        if len(ts) < BUCKET_MIN_TRADES:
            continue
        wins = [t for t in ts if t["won"]]
        losses = [t for t in ts if not t["won"]]
        wr = len(wins) / len(ts)
        avg_win = sum(t["pnl_usd"] for t in wins) / max(len(wins), 1)
        avg_loss = sum(t["pnl_usd"] for t in losses) / max(len(losses), 1)
        ev = avg_win * wr + avg_loss * (1 - wr)
        results[label] = {
            "trade_count": len(ts),
            "wins": len(wins),
            "losses": len(losses),
            "true_wr": round(wr, 4),
            "avg_win_usd": round(avg_win, 4),
            "avg_loss_usd": round(avg_loss, 4),
            "true_ev_per_trade": round(ev, 4),
        }
    return results


def oos_split(trades: List[Dict], patterns: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    60/40 chronological train/test split.
    Returns patterns enriched with oos_ev and passes_oos flag.
    """
    split_idx = int(len(trades) * 0.6)
    oos_trades = trades[split_idx:]
    oos_patterns = extract_patterns(oos_trades)

    enriched = {}
    for label, stats in patterns.items():
        oos = oos_patterns.get(label)
        oos_ev = oos["true_ev_per_trade"] if oos else None
        enriched[label] = {
            **stats,
            "oos_ev": oos_ev,
            "oos_trade_count": oos["trade_count"] if oos else 0,
            "passes_oos": oos_ev is not None and oos_ev > MIN_EV_USD,
        }
    return enriched


# ── Cross-wallet synthesis ────────────────────────────────────────────────────

def cross_wallet_patterns(all_wallet_patterns: Dict[str, Dict]) -> List[Dict]:
    """
    Find (market_type, price_bucket) combos that appear with EV > MIN_EV_USD
    across ≥ 3 independent wallets.
    """
    bucket_wallets: Dict[str, List[Dict]] = defaultdict(list)

    for wallet, patterns in all_wallet_patterns.items():
        for label, stats in patterns.items():
            if stats.get("true_ev_per_trade", 0) > MIN_EV_USD:
                bucket_wallets[label].append({"wallet": wallet, **stats})

    results = []
    for label, wallet_list in bucket_wallets.items():
        if len(wallet_list) < 3:
            continue
        total_trades = sum(w["trade_count"] for w in wallet_list)
        avg_ev = sum(w["true_ev_per_trade"] for w in wallet_list) / len(wallet_list)
        avg_wr = sum(w["true_wr"] for w in wallet_list) / len(wallet_list)
        oos_passing = [w for w in wallet_list if w.get("passes_oos")]
        parts = label.split("|")
        results.append({
            "bucket_label": label,
            "market_type": parts[0] if parts else "",
            "price_bucket": parts[1] if len(parts) > 1 else "",
            "wallet_count": len(wallet_list),
            "total_trades": total_trades,
            "avg_ev_per_trade": round(avg_ev, 4),
            "avg_true_wr": round(avg_wr, 4),
            "oos_passing_wallets": len(oos_passing),
            "passes_cross_wallet": len(oos_passing) >= 3,
            "wallets": [w["wallet"] for w in wallet_list],
        })

    return sorted(results, key=lambda p: p["avg_ev_per_trade"], reverse=True)


# ── Edge decay check ──────────────────────────────────────────────────────────

def detect_decay(prev_path: str, new_patterns: List[Dict]) -> List[Dict]:
    """Compare new cross-wallet patterns to the most recent prior scan."""
    with open(prev_path) as f:
        prev = json.load(f)

    prev_by_label = {p["bucket_label"]: p for p in prev.get("cross_wallet_patterns", [])}
    alerts = []
    for p in new_patterns:
        label = p["bucket_label"]
        prev_p = prev_by_label.get(label)
        if prev_p is None:
            continue
        old_ev = prev_p.get("avg_ev_per_trade", 0)
        new_ev = p.get("avg_ev_per_trade", 0)
        if old_ev <= 0:
            continue
        drop = (old_ev - new_ev) / old_ev
        if drop > EV_DECAY_THRESHOLD or new_ev < MIN_EV_USD:
            severity = "HIGH" if drop > 0.50 else "MEDIUM"
            alerts.append({
                "pattern": label,
                "old_ev": round(old_ev, 4),
                "new_ev": round(new_ev, 4),
                "pct_drop": round(drop, 3),
                "severity": severity,
            })
    return alerts


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    client = PolymarketPublicClient(user_agent="polywhale-edge-scan/1.0")

    print("[scan] Fetching top wallets from lb-api...")
    wallets = fetch_top_wallets(TOP_N)
    print(f"[scan] {len(wallets)} wallets qualify (≥${MIN_TRUE_PROFIT_USD} realized)")

    all_wallet_patterns: Dict[str, Dict] = {}
    wallet_stats = []

    for i, w in enumerate(wallets):
        addr = w["proxy_wallet"]
        if not addr:
            continue
        print(f"[scan] [{i+1}/{len(wallets)}] {addr[:12]}... (lb profit=${w['lb_profit_usd']:.0f})")

        try:
            events = fetch_full_activity(client, addr)
            trades = reconstruct_trades(events)
            if len(trades) < MIN_TRADES:
                print(f"         skip — only {len(trades)} trades reconstructed")
                continue
            patterns = extract_patterns(trades)
            patterns = oos_split(trades, patterns)
            all_wallet_patterns[addr] = patterns

            wins = [t for t in trades if t["won"]]
            losses = [t for t in trades if not t["won"]]
            wr = len(wins) / len(trades) if trades else 0
            avg_ev = sum(t["pnl_usd"] for t in trades) / len(trades) if trades else 0

            wallet_stats.append({
                "wallet": addr,
                "lb_profit_usd": w["lb_profit_usd"],
                "total_trades": len(trades),
                "true_wr": round(wr, 4),
                "true_ev_per_trade": round(avg_ev, 4),
                "pattern_count": len(patterns),
            })
            print(f"         trades={len(trades)} WR={wr:.1%} EV=${avg_ev:.3f} patterns={len(patterns)}")
        except Exception as e:
            print(f"         error: {e}")
            continue

    print(f"\n[scan] Analyzed {len(all_wallet_patterns)} wallets with ≥{MIN_TRADES} trades")

    # Cross-wallet synthesis
    cross_patterns = cross_wallet_patterns(all_wallet_patterns)
    print(f"[scan] {len(cross_patterns)} cross-wallet patterns (≥3 wallets with EV>${MIN_EV_USD})")
    if cross_patterns:
        print("\nTop cross-wallet patterns:")
        for p in cross_patterns[:10]:
            oos_flag = "✓OOS" if p["passes_cross_wallet"] else "  "
            print(f"  {oos_flag} {p['bucket_label']:20s}  wallets={p['wallet_count']}  ev=${p['avg_ev_per_trade']:.3f}  wr={p['avg_true_wr']:.1%}")

    # Save findings
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(DATA_DIR, f"edge_scan_{ts}.json")
    findings = {
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
        "wallets_analyzed": len(all_wallet_patterns),
        "wallet_stats": wallet_stats,
        "cross_wallet_patterns": cross_patterns,
        "decay_alerts": [],
    }

    # Edge decay check
    prior_scans = sorted(glob.glob(os.path.join(DATA_DIR, "edge_scan_*.json")))
    if prior_scans:
        prev_path = prior_scans[-1]
        print(f"\n[scan] Decay check vs {os.path.basename(prev_path)}")
        try:
            alerts = detect_decay(prev_path, cross_patterns)
            findings["decay_alerts"] = alerts
            if alerts:
                print(f"⚠️  {len(alerts)} edge decay alerts:")
                for a in alerts:
                    icon = "🔴" if a["severity"] == "HIGH" else "🟡"
                    print(f"  {icon} {a['pattern']}: ${a['old_ev']:.3f} → ${a['new_ev']:.3f} ({a['pct_drop']:.0%} drop)")
            else:
                print("✅ No edge decay detected.")
        except Exception as e:
            print(f"[scan] decay check failed: {e}")

    with open(out_path, "w") as f:
        json.dump(findings, f, indent=2)
    print(f"\n[scan] findings written to {out_path}")

    # GitHub Actions step summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"## Edge Scan — {findings['date']}\n\n")
            f.write(f"- Wallets analyzed: **{findings['wallets_analyzed']}**\n")
            f.write(f"- Cross-wallet patterns: **{len(cross_patterns)}**\n\n")
            if cross_patterns:
                f.write("| Pattern | Wallets | EV/trade | WR | OOS |\n")
                f.write("|---|---|---|---|---|\n")
                for p in cross_patterns[:10]:
                    oos = "✓" if p["passes_cross_wallet"] else "—"
                    f.write(f"| {p['bucket_label']} | {p['wallet_count']} | ${p['avg_ev_per_trade']:.3f} | {p['avg_true_wr']:.1%} | {oos} |\n")
            alerts = findings.get("decay_alerts", [])
            if alerts:
                f.write(f"\n### ⚠️ Edge Decay Alerts\n\n")
                for a in alerts:
                    f.write(f"- **{a['severity']}** `{a['pattern']}`: ${a['old_ev']:.3f} → ${a['new_ev']:.3f} ({a['pct_drop']:.0%} drop)\n")

    # Exit code 2 if HIGH decay detected (used by CI to open GitHub issue)
    high_alerts = [a for a in findings.get("decay_alerts", []) if a["severity"] == "HIGH"]
    if high_alerts:
        print(f"\n[scan] EXIT 2 — {len(high_alerts)} HIGH severity decay alerts")
        sys.exit(2)


if __name__ == "__main__":
    main()
