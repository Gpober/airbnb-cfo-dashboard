#!/usr/bin/env python3
"""
perps_backtest.py — fetch REAL hourly BTC history and backtest perp strategies.

Runs where there's open internet (e.g. Railway) -- NOT in the locked-down dev
sandbox. Pulls public hourly candles from Coinbase (no API key needed), then
runs every strategy in ``perps_sim`` against the real series, benchmarked
against buy & hold, with an out-of-sample split so a strategy can't look good
just by fitting one regime. Prints a ranked report to stdout (your Railway logs).

This is the ONLY way to hunt for a genuine edge: random paths are a no-edge
world by construction. Beating buy & hold on real history (and holding up
out-of-sample) is the bar.

Usage (e.g. Railway custom start command / one-off):
    python perps_backtest.py --hours 2000 --leverage 3
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from typing import List, Tuple

import requests

from perps_sim import STRATEGIES, SimConfig, simulate, SimResult

COINBASE = "https://api.exchange.coinbase.com"


def parse_candles(rows) -> List[Tuple[int, float]]:
    """Coinbase candle = [time, low, high, open, close, volume]; return (time, close)."""
    out: List[Tuple[int, float]] = []
    for r in rows:
        try:
            out.append((int(r[0]), float(r[4])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def fetch_coinbase_hourly(hours: int = 2000, product: str = "BTC-USD",
                          session=None, log=print) -> List[float]:
    """Page Coinbase's public candles endpoint backwards until we have `hours`."""
    session = session or requests.Session()
    gran = 3600
    end = int(time.time())
    got: dict = {}
    guard = 0
    while len(got) < hours and guard < 200:
        guard += 1
        start = end - 300 * gran
        params = {
            "granularity": gran,
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        }
        try:
            resp = session.get(f"{COINBASE}/products/{product}/candles",
                               params=params, timeout=30,
                               headers={"User-Agent": "perps-backtest/1.0"})
        except requests.RequestException as exc:
            log(f"fetch error: {exc}")
            break
        if resp.status_code != 200:
            log(f"fetch stopped: HTTP {resp.status_code} {resp.text[:120]}")
            break
        rows = parse_candles(resp.json())
        if not rows:
            break
        for t, c in rows:
            got[t] = c
        end = start
        log(f"  fetched {len(got)} hourly candles...")
        time.sleep(0.34)  # be polite to the public API
    return [c for _, c in sorted(got.items())][-hours:]


def _buy_and_hold(prices: List[float]) -> SimResult:
    return simulate(prices, lambda p, s: 1,
                    SimConfig(leverage=1.0, fee_rate=0.0, funding_rate_per_step=0.0))


def report(prices: List[float], leverage: float, log=print) -> dict:
    n = len(prices)
    if n < 100:
        log(f"not enough data ({n} candles); need >=100")
        return {}
    split = int(n * 0.7)
    bh = _buy_and_hold(prices)

    log(f"\n=== REAL BTC BACKTEST — {n} hourly candles (~{n / 24:.0f} days) ===")
    log(f"buy & hold (1x, no leverage):  {bh.return_pct:+.1f}%   <- the bar to beat")
    log(f"leverage tested:               {leverage}x\n")
    log(f"{'strategy':10} {'return':>8}  {'win':>4}  {'maxDD':>6}  {'liq':>3}  {'out-of-sample':>13}  flag")
    log("-" * 72)

    rows = []
    for name, strat in STRATEGIES.items():
        if name == "flat":
            continue
        full = simulate(prices, strat, SimConfig(leverage=leverage))
        oos = simulate(prices[split:], strat, SimConfig(leverage=leverage))
        rows.append((name, full, oos))
    rows.sort(key=lambda x: x[1].return_pct, reverse=True)

    winners = []
    for name, full, oos in rows:
        wr = "n/a" if full.win_rate is None else f"{full.win_rate*100:.0f}%"
        beats = full.return_pct > bh.return_pct and full.return_pct > 0
        holds = oos.return_pct > 0
        flag = "BEATS B&H + OOS+" if beats and holds else ("beats B&H" if beats else "")
        if beats and holds:
            winners.append(name)
        log(f"{name:10} {full.return_pct:+7.1f}%  {wr:>4}  {full.max_drawdown_pct:5.0f}%  "
            f"{full.liquidations:3d}  {oos.return_pct:+12.1f}%  {flag}")

    log("-" * 72)
    if winners:
        log(f"VERDICT: {', '.join(winners)} beat buy & hold AND stayed positive "
            f"out-of-sample. Promising — validate on more history/leverage before ANY live risk.")
    else:
        log("VERDICT: NO strategy beat buy & hold while holding up out-of-sample. "
            "No demonstrable edge on this data. Do NOT trade perps live.")
    return {"buy_hold_pct": bh.return_pct, "winners": winners,
            "results": {name: full.return_pct for name, full, _ in rows}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest perp strategies on real BTC history")
    p.add_argument("--hours", type=int, default=2000, help="hours of history (~83 days=2000)")
    p.add_argument("--leverage", type=float, default=3.0)
    p.add_argument("--product", default="BTC-USD")
    args = p.parse_args(argv)

    print(f"Fetching ~{args.hours}h of {args.product} from Coinbase...")
    prices = fetch_coinbase_hourly(args.hours, args.product)
    if len(prices) < 100:
        print(f"ERROR: only got {len(prices)} candles; cannot backtest.")
        return 1
    report(prices, args.leverage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
