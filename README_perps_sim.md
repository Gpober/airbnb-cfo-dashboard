# Perps strategy simulator (`perps_sim.py`)

A **simulation-only** harness for testing perpetual-futures strategies before
any live perps trading exists. No network, no exchange, no money. It's step 1 of
the responsible path to trading perps:

1. **Prove an edge here** (positive expectancy in simulation, after fees + funding).
2. Then wire Kalshi's perps API + fund the perpetual account.
3. Then trade tiny, and only scale if live results track the sim.

If a strategy loses money against this *friendly* simulator — perfect fills, no
slippage — it will lose money live, faster. So this is the cheap truth-teller.

## What it models
- **Leverage** (position sizing off account equity)
- **Trading fees** (bps per side, on notional)
- **Funding** payments (longs pay shorts each step)
- **Liquidation** (position wiped when equity can't cover maintenance margin)
- **Drawdown, win rate, ruin rate** across many random price paths

## Run it

```bash
# No real edge (fair random walk) -> expect "NO DEMONSTRABLE EDGE"
python perps_sim.py --strategy sma --leverage 3 --paths 300

# Add a real trend and see if the strategy can actually capture it
python perps_sim.py --strategy meanrev --leverage 5 --mu 0.4

# Crank leverage to see liquidations/ruin appear
python perps_sim.py --strategy sma --leverage 15 --paths 300
```

Strategies included: `flat` (control), `sma` (trend-following), `meanrev`
(fade extremes). Add your own by writing a `(price_history, side) -> {-1,0,1}`
function and registering it in `STRATEGIES`.

## The honest headline
On a zero-drift random walk, every built-in strategy shows **no demonstrable
edge** — fees and funding bleed it, and leverage just adds liquidations. That's
not a bug; it's the lesson. A perps bot only makes sense once a strategy clears
this bar convincingly, and even then the sim is necessary but not sufficient.

## What this is NOT
It is **not** a live trading bot and places **no** orders. Live perps would be a
separate module gated behind the same `DEMO`/`DRY_RUN` safety as the binary bot,
plus a proven strategy and a funded perpetual account.
