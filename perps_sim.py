#!/usr/bin/env python3
"""
perps_sim.py — Perpetual-futures strategy SIMULATOR (no network, no money).

Purpose: test perp trading ideas against price paths with the frictions that
actually decide whether a leveraged strategy survives -- trading fees, funding
payments, and LIQUIDATION -- before a single real dollar or line of live-order
code exists. This module NEVER touches an exchange.

Live perps trading is deliberately NOT implemented here. Going live needs three
things, in order: (1) a strategy with PROVEN positive expectancy in simulation,
(2) Kalshi's perps API + a funded perpetual account, (3) tiny size first. Step 1
is this file's whole job. If a strategy loses money here -- against a friendly
simulator with no slippage and perfect fills -- it will lose money live, faster.

Honest default lesson: on a zero-drift random walk (no real edge), every
strategy here bleeds out through fees + funding, and higher leverage just adds
liquidations. That is the point. The simulator is here to tell you the truth
cheaply.

CLI:
  python perps_sim.py --strategy sma --leverage 3 --paths 300
  python perps_sim.py --strategy meanrev --leverage 5 --mu 0.3   # add drift
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# --------------------------------------------------------------------------- #
# Price paths
# --------------------------------------------------------------------------- #


def gbm_path(n: int, s0: float = 60_000.0, mu: float = 0.0,
             sigma_annual: float = 0.6, dt_hours: float = 1.0,
             rng: Optional[random.Random] = None) -> List[float]:
    """Geometric-Brownian-motion price path (n steps of dt_hours each).

    ``mu`` is annualized drift (0 = a fair coin, the honest default), ``sigma``
    annualized volatility (~0.6 is a reasonable BTC figure).
    """
    rng = rng or random.Random()
    dt = dt_hours / (365.0 * 24.0)
    drift = (mu - 0.5 * sigma_annual ** 2) * dt
    vol = sigma_annual * math.sqrt(dt)
    price = s0
    path = [price]
    for _ in range(n):
        price *= math.exp(drift + vol * rng.gauss(0.0, 1.0))
        path.append(price)
    return path


# --------------------------------------------------------------------------- #
# Strategies: (price_history, current_side) -> desired_side in {-1, 0, +1}
# --------------------------------------------------------------------------- #

Strategy = Callable[[List[float], int], int]


def strat_flat(prices: List[float], side: int) -> int:
    return 0


def strat_sma(prices: List[float], side: int, fast: int = 12, slow: int = 48) -> int:
    """Trend-following: long above the slow SMA, short below."""
    if len(prices) < slow:
        return 0
    f = sum(prices[-fast:]) / fast
    s = sum(prices[-slow:]) / slow
    return 1 if f > s else -1


def strat_meanrev(prices: List[float], side: int, window: int = 48, z: float = 1.5) -> int:
    """Mean-reversion: fade extremes, exit when price crosses back to the mean."""
    if len(prices) < window:
        return 0
    w = prices[-window:]
    m = sum(w) / window
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / window) or 1e-9
    zscore = (prices[-1] - m) / sd
    if side == 0:
        if zscore > z:
            return -1
        if zscore < -z:
            return 1
        return 0
    if side == 1 and zscore >= 0:
        return 0        # reverted up -> exit long
    if side == -1 and zscore <= 0:
        return 0        # reverted down -> exit short
    return side


def strat_breakout(prices: List[float], side: int, lookback: int = 24) -> int:
    """Donchian breakout: long on a new N-bar high, short on a new N-bar low."""
    if len(prices) < lookback + 1:
        return 0
    window = prices[-(lookback + 1):-1]
    p = prices[-1]
    if p > max(window):
        return 1
    if p < min(window):
        return -1
    return side            # hold inside the channel


def strat_momentum(prices: List[float], side: int,
                   lookback: int = 24, thresh: float = 0.01) -> int:
    """Momentum: long if price rose > thresh over the lookback, short if it fell."""
    if len(prices) < lookback + 1:
        return 0
    roc = prices[-1] / prices[-1 - lookback] - 1.0
    if roc > thresh:
        return 1
    if roc < -thresh:
        return -1
    return 0


STRATEGIES = {
    "flat": strat_flat, "sma": strat_sma, "meanrev": strat_meanrev,
    "breakout": strat_breakout, "momentum": strat_momentum,
}


def load_prices_csv(path: str, column: str = "close") -> List[float]:
    """Load a price series from a CSV (header row; picks the ``close`` column,
    else the first numeric column). Real historical data is the ONLY way this
    sim can discover a genuine edge -- see the note in run_batch/main."""
    import csv
    prices: List[float] = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None) or []
        low = [h.strip().lower() for h in header]
        idx = low.index(column) if column in low else 0
        # If the "header" was actually numeric data, keep it.
        try:
            prices.append(float(header[idx]))
        except (ValueError, IndexError):
            pass
        for row in reader:
            try:
                prices.append(float(row[idx]))
            except (ValueError, IndexError):
                continue
    return prices


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #


@dataclass
class SimConfig:
    starting_equity: float = 1000.0
    leverage: float = 3.0
    max_leverage: float = 20.0
    fee_rate: float = 0.0005            # 5 bps taker fee per side, on notional
    funding_rate_per_step: float = 0.0001   # longs pay shorts each step (~1bp/hr)
    maintenance_rate: float = 0.005     # 0.5% maintenance margin -> liquidation
    risk_fraction: float = 1.0          # fraction of equity backing each trade


@dataclass
class SimResult:
    final_equity: float
    return_pct: float
    trades: int
    wins: int
    win_rate: Optional[float]
    max_drawdown_pct: float
    liquidations: int
    fees_paid: float
    funding_paid: float
    equity_curve: List[float] = field(default_factory=list)


def simulate(prices: List[float], strategy: Strategy,
             cfg: Optional[SimConfig] = None) -> SimResult:
    """Run one price path through a strategy with a cross-margin perp model."""
    cfg = cfg or SimConfig()
    lev = min(cfg.leverage, cfg.max_leverage)

    wallet = cfg.starting_equity   # realized cash
    side = 0                       # -1 short, 0 flat, +1 long
    size = 0.0                     # BTC units
    entry = 0.0
    fees = funding = 0.0
    trades = wins = liquidations = 0
    peak = wallet
    max_dd = 0.0
    curve: List[float] = []

    def equity(price: float) -> float:
        upnl = side * size * (price - entry) if side != 0 else 0.0
        return wallet + upnl

    def close(price: float) -> None:
        nonlocal wallet, side, size, entry, fees, trades, wins
        if side == 0:
            return
        upnl = side * size * (price - entry)
        fee = size * price * cfg.fee_rate
        wallet += upnl - fee
        fees += fee
        trades += 1
        if upnl - fee > 0:
            wins += 1
        side = size = 0
        entry = 0.0

    def open_pos(new_side: int, price: float) -> None:
        nonlocal wallet, side, size, entry, fees
        eq = max(0.0, equity(price))
        notional = eq * cfg.risk_fraction * lev
        if notional <= 0 or price <= 0:
            return
        size = notional / price
        fee = notional * cfg.fee_rate
        wallet -= fee
        fees += fee
        side = new_side
        entry = price

    for i, price in enumerate(prices):
        # 1) funding on any open position
        if side != 0:
            pay = cfg.funding_rate_per_step * (size * price) * side  # long pays if +
            wallet -= pay
            funding += pay

        # 2) liquidation: account value can't cover maintenance margin
        if side != 0 and equity(price) <= cfg.maintenance_rate * (size * price):
            wallet = max(0.0, equity(price) - cfg.maintenance_rate * (size * price))
            side = size = 0
            entry = 0.0
            trades += 1
            liquidations += 1

        # 3) strategy decision
        target = strategy(prices[: i + 1], side)
        if target != side:
            close(price)
            if target != 0:
                open_pos(target, price)

        eq = equity(price)
        curve.append(eq)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        if eq <= 0:              # ruin: stop trading
            side = size = 0
            entry = 0.0

    close(prices[-1])
    final = wallet
    return SimResult(
        final_equity=final,
        return_pct=(final / cfg.starting_equity - 1.0) * 100.0,
        trades=trades,
        wins=wins,
        win_rate=(wins / trades) if trades else None,
        max_drawdown_pct=max_dd * 100.0,
        liquidations=liquidations,
        fees_paid=fees,
        funding_paid=funding,
        equity_curve=curve,
    )


# --------------------------------------------------------------------------- #
# Batch (many paths) + reporting
# --------------------------------------------------------------------------- #


def run_batch(strategy: Strategy, cfg: SimConfig, paths: int = 300,
              steps: int = 168, s0: float = 60_000.0, sigma: float = 0.6,
              mu: float = 0.0, seed: int = 1) -> dict:
    """Simulate many independent GBM paths; return aggregate stats."""
    rng = random.Random(seed)
    results = [simulate(gbm_path(steps, s0=s0, mu=mu, sigma_annual=sigma, rng=rng),
                        strategy, cfg) for _ in range(paths)]
    rets = sorted(r.return_pct for r in results)
    n = len(results)
    mean_ret = sum(rets) / n
    median_ret = rets[n // 2]
    liq_paths = sum(1 for r in results if r.liquidations > 0)
    ruined = sum(1 for r in results if r.final_equity <= 0.01)
    profitable = sum(1 for r in results if r.return_pct > 0)
    return {
        "paths": n, "mean_return_pct": mean_ret, "median_return_pct": median_ret,
        "profitable_pct": profitable / n * 100.0,
        "liquidated_pct": liq_paths / n * 100.0,
        "ruined_pct": ruined / n * 100.0,
        "avg_trades": sum(r.trades for r in results) / n,
        "avg_max_drawdown_pct": sum(r.max_drawdown_pct for r in results) / n,
        "avg_fees": sum(r.fees_paid for r in results) / n,
        "avg_funding": sum(r.funding_paid for r in results) / n,
    }


def print_report(name: str, cfg: SimConfig, agg: dict, mu: float) -> None:
    print(f"\n=== PERPS SIM: {name}  (leverage={cfg.leverage}x, drift mu={mu}) ===")
    print(f"paths                 {agg['paths']}")
    print(f"mean return           {agg['mean_return_pct']:+.1f}%")
    print(f"median return         {agg['median_return_pct']:+.1f}%")
    print(f"profitable paths      {agg['profitable_pct']:.0f}%")
    print(f"paths with a liquidation  {agg['liquidated_pct']:.0f}%")
    print(f"paths fully ruined    {agg['ruined_pct']:.0f}%")
    print(f"avg trades / path     {agg['avg_trades']:.1f}")
    print(f"avg max drawdown      {agg['avg_max_drawdown_pct']:.0f}%")
    print(f"avg fees paid         ${agg['avg_fees']:.2f}")
    print(f"avg funding paid      ${agg['avg_funding']:+.2f}")
    # Median + hit-rate, not the fat-tail-prone mean: a couple of lucky
    # trend-riders can drag the mean positive on a pure coin flip.
    demonstrable = agg["median_return_pct"] > 0 and agg["profitable_pct"] > 55.0
    verdict = ("positive in sim — worth deeper testing, still not proven live"
               if demonstrable else
               "NO DEMONSTRABLE EDGE — indistinguishable from fees/noise; do not trade live")
    print(f"VERDICT: {verdict}")


GBM_CAVEAT = (
    "NOTE: random (GBM) paths are a NO-EDGE world by construction -- no strategy\n"
    "can beat them beyond luck. That makes this great for REJECTING strategies and\n"
    "measuring fee/funding/leverage drag, but it can NEVER discover a real edge.\n"
    "To hunt for an actual edge, backtest on REAL price history:  --prices data.csv"
)


def print_single(name: str, cfg: SimConfig, r: SimResult) -> None:
    print(f"\n=== PERPS SIM (real data): {name}  (leverage={cfg.leverage}x) ===")
    print(f"final equity          ${r.final_equity:.2f}  ({r.return_pct:+.1f}%)")
    print(f"trades                {r.trades}  (win rate "
          f"{'n/a' if r.win_rate is None else f'{r.win_rate:.0%}'})")
    print(f"max drawdown          {r.max_drawdown_pct:.0f}%")
    print(f"liquidations          {r.liquidations}")
    print(f"fees / funding paid   ${r.fees_paid:.2f} / ${r.funding_paid:+.2f}")
    print(f"VERDICT: {'PROFITABLE on this history (one sample -- validate out-of-sample)' if r.return_pct > 0 else 'loses on this history'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Perpetual-futures strategy simulator")
    p.add_argument("--strategy", choices=list(STRATEGIES), default="sma")
    p.add_argument("--leverage", type=float, default=3.0)
    p.add_argument("--paths", type=int, default=300)
    p.add_argument("--steps", type=int, default=168, help="price steps (hours); 168=1wk")
    p.add_argument("--mu", type=float, default=0.0, help="annualized drift (0=fair)")
    p.add_argument("--sigma", type=float, default=0.6, help="annualized volatility")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--prices", help="CSV of real historical prices to backtest on")
    args = p.parse_args(argv)

    cfg = SimConfig(leverage=args.leverage)

    if args.prices:
        prices = load_prices_csv(args.prices)
        if len(prices) < 50:
            print(f"need >=50 price points, got {len(prices)}")
            return 1
        r = simulate(prices, STRATEGIES[args.strategy], cfg)
        print_single(args.strategy, cfg, r)
        return 0

    agg = run_batch(STRATEGIES[args.strategy], cfg, paths=args.paths,
                    steps=args.steps, sigma=args.sigma, mu=args.mu, seed=args.seed)
    print_report(args.strategy, cfg, agg, args.mu)
    print("\n" + GBM_CAVEAT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
