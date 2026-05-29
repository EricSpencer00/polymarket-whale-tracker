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

The headline command:

```bash
# 1. Find the BTC whales with conviction + edge, then live-stream their trades.
polywhale autopilot --asset BTC
```

`autopilot` chains the whole pipeline in one shot: scans recent fast crypto
markets for active scalpers, drops anyone with tiny sprayed clips, drops
anyone whose reconstructed PnL isn't provably positive and steady, persists
the qualified watchlist to `data/latest_btc.json`, and then opens the
public WSS feed and streams `CopySignal` events from those wallets as they
trade. Each signal carries a `style_hint` reflecting the source wallet's
qualified profile (`steady_high_conviction`, `steady_low_conviction`, etc).

Filters worth knowing:

| Flag | What it does | Default |
|------|--------------|---------|
| `--hours` | Scan-fast look-back window. | `6` |
| `--days` | PnL-trajectory window. | `7` |
| `--min-volume` | Drop wallets with < $N USDC volume in the scan window. | `1500` |
| `--min-steadiness` | Composite PnL steadiness score (0-1). | `0.45` |
| `--min-conviction` | Composite conviction score (clip size + repeat-pattern). | `0.20` |
| `--min-clip` | Drop wallets whose median clip is below this. | `0` |
| `--max-idle-minutes` | Drop wallets that haven't traded recently. | `240` |
| `--top` | Cap the watchlist to N wallets. | `10` |
| `--no-watch` | Stop after persistence; don't open the WSS stream. | off |

Other commands (all `--json`-able):

```bash
polywhale resolve boneweeper                       # handle -> address
polywhale discover --asset BTC --top 20            # top holders by USD exposure
polywhale scan-fast --asset BTC --hours 6          # volume in 5m/15m markets
polywhale analyze 0xb17a...                        # algo-behavior fingerprint
polywhale pnl 0xb17a... --days 14                  # reconstructed PnL curve
polywhale qualify --asset BTC --min-steadiness 0.5 # scan-fast + PnL filter only
polywhale macro --asset BTC                        # market-landscape snapshot
polywhale watch 0xb17a... 0xc790...                # raw WSS stream from wallets
```

## Architecture

```
polywhale/
  api.py        REST client: data-api + gamma-api + clob
  ws.py         WSS client: ws-live-data
  fast.py       fast-market detector (5m/15m Up-or-Down)
  discover.py   top holders by USD exposure (slow markets)
  scan.py       USDC-volume aggregator for fast markets
  analyze.py    algorithmic-behavior fingerprinter
  pnl.py        PnL trajectory + steadiness metrics
  conviction.py clip-size + repeat-pattern conviction scorer
  macro.py      market-landscape snapshot
  watch.py      live signal emitter (CopySignal stream)
  signal.py     CopySignal dataclass
  persist.py    watchlist persistence to $POLYWHALE_DATA_DIR
  autopilot.py  end-to-end qualify pipeline
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
