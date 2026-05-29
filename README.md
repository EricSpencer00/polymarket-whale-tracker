# polymarket-whale-tracker

Discover and analyze algorithmic whales on Polymarket; emit copy-trade signals.

The tool is **read-only**. It never places, modifies, or signs orders. All it
does is pull public Polymarket data, score wallets for algorithmic behavior,
and emit structured signals that a downstream executor (which lives in a
separate repo with its own keys) can choose to act on.

## What it does

- **Discover** — scan live Bitcoin / Ethereum / Solana markets, pull the top
  holders, surface wallets with large concentrated exposure.
- **Analyze** — fingerprint a wallet's trading style from its activity feed:
  sub-second clustering, both-sides hedging, sizing entropy, BUY/SELL ratio,
  market concentration. Outputs an `algo_score` (0–1) and a style label
  (`hf_scalper`, `market_maker`, `swing`, `degen`).
- **Macro** — snapshot the BTC-market landscape: volume, open interest,
  spread, time-to-resolution. Helps you sanity-check a copy signal against
  the broader market.
- **Watch** — open a single WebSocket to `ws-live-data.polymarket.com`,
  filter trades by your watchlist, emit `CopySignal` events in real time.

## Why

Polymarket has a small population of clearly algorithmic accounts running
high-frequency strategies on 5-minute and 15-minute crypto "Up or Down"
markets. The good ones leave a recognizable fingerprint: dozens of trades
per minute, deep two-sided books on the same `conditionId`, machine-tight
sizing. The goal here is to identify them, characterize their style, and
extract a clean directional signal that a separate executor can copy at
small size for incremental edge.

## Install

```bash
git clone https://github.com/EricSpencer00/polymarket-whale-tracker.git
cd polymarket-whale-tracker
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Python 3.10+. The only runtime deps are `requests` and `websockets`.

## Quickstart

```bash
# Resolve a handle to an address
polywhale resolve boneweeper

# Find the biggest wallets across BTC markets by USD exposure
polywhale discover --asset BTC --top 20

# Rank wallets by USDC volume in fast (5m/15m) BTC markets over the last 6h
polywhale scan-fast --asset BTC --hours 6 --top 25

# Fingerprint a single wallet (algorithmic-behavior score + style)
polywhale analyze 0xb17a1076a5ce053bd117a6eb51b309678d26f7e5

# Reconstruct a wallet's PnL trajectory (auto-buckets hourly vs daily)
polywhale pnl 0xb17a1076a5ce053bd117a6eb51b309678d26f7e5 --days 14

# scan-fast -> PnL filter -> ranked watchlist of steady winners
polywhale qualify --asset BTC --hours 6 --candidates 25 --min-steadiness 0.5

# Macro snapshot of BTC markets
polywhale macro --asset BTC

# Stream copy signals from a watchlist (Ctrl-C to stop)
polywhale watch 0xb17a1076a5ce053bd117a6eb51b309678d26f7e5
```

Add `--json` to any command for machine-readable output.

`qualify` is the main entry point for copy-trade prep: it scans recent fast
markets, ranks wallets by USDC volume, reconstructs each candidate's PnL
trajectory, and outputs the subset with positive realized PnL and high
steadiness (slope, R^2 of the trend line, small drawdowns, mostly winning
days/hours).

## Architecture

```
polywhale/
  api.py        REST client: data-api + gamma-api + clob
  ws.py         WSS client: ws-live-data
  fast.py       fast-market detector (5m/15m Up-or-Down)
  discover.py   find top holders of a market / asset (USD exposure)
  scan.py       rank wallets by USDC volume in fast markets
  analyze.py    algorithmic-behavior fingerprinter
  pnl.py        PnL trajectory + steadiness metrics
  macro.py      market-landscape snapshot
  watch.py      live signal emitter (CopySignal stream)
  signal.py     CopySignal dataclass
  cli.py        argparse entrypoint
```

The REST + WSS clients are thin and synchronous; the watcher uses
`asyncio` for the socket only. Nothing requires authentication; the
public Polymarket endpoints handle everything.

## What this repo does NOT do

- It does **not** place orders.
- It does **not** hold or use a private key.
- It does **not** ship an executor. Pipe the signal stream to whatever
  separate, keyed bot you trust.

That separation is deliberate: this whole repo is safe to be public.

## Disclaimer

Not financial advice. Prediction markets are speculative and you can lose
money. Copy-trading inherits the followee's risk plus your own latency
and slippage; you'll almost always do worse than the source wallet. Use
at your own risk.
