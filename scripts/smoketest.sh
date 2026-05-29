#!/usr/bin/env bash
# Live smoke test — hits the public Polymarket APIs read-only. Safe to run.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== resolve handles =="
python -m polywhale resolve boneweeper
python -m polywhale resolve purple-lamp-tree

echo
echo "== analyze W2 (known BTC market-maker) =="
python -m polywhale analyze 0xb17a1076a5ce053bd117a6eb51b309678d26f7e5

echo
echo "== analyze W1 (known both-sides scalper) =="
python -m polywhale analyze 0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82

echo
echo "== BTC macro snapshot =="
python -m polywhale macro --asset BTC --limit 10

echo
echo "== discover BTC whales =="
python -m polywhale discover --asset BTC --top 10 --min-exposure 250
