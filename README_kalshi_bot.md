# Kalshi Hourly BTC Bot (`KXBTCD`)

A small, single-module trading bot for Kalshi's hourly Bitcoin *above/below*
contracts. It ships **DEMO + DRY_RUN** and will not touch real money unless you
explicitly turn both safety switches off (see the go-live checklist).

- **`kalshi_btc_hourly_bot.py`** — the bot (auth, REST client, strategy, paper
  harness, WebSocket feed, robustness).
- **`kalshi_selftest.py`** — offline self-tests (no network required).

## Strategy

- Resolve the *live* market ticker from `GET /markets` each run (hourly markets
  rotate — the ticker is never hard-coded).
- **Entry:** buy YES when the YES ask is **85–90¢**, sizing each entry to ~**$1000**.
- **Stop-loss:** sell when the YES bid falls to `entry − STOP_LOSS_CENTS` (default 1¢).
- **Take-profit:** sell at **99¢**.
- **Scale-out (optional):** sell a fraction once the bid reaches an intermediate price.

## Install

```bash
pip install -r requirements.txt
```

`websocket-client` is optional; without it, the management loop automatically
uses REST polling.

## Usage

```bash
# Offline self-tests — no network, no credentials needed.
python kalshi_btc_hourly_bot.py --selftest

# Resolve the active ticker and print top-of-book (needs API reachable).
python kalshi_btc_hourly_bot.py

# Paper-trade against the LIVE demo book, simulate fills, log to SQLite.
python kalshi_btc_hourly_bot.py --paper --cycles 300 --interval 5

# Print the summary from the SQLite log at any time.
python kalshi_btc_hourly_bot.py --report

# WebSocket-driven management loop (REST fallback), still dry-run by default.
python kalshi_btc_hourly_bot.py --manage
```

### Paper-trade report

After a `--paper` run (or via `--report`) the bot prints:

```
 Trades                 : N
 Win rate               : %
 Avg P&L / trade        : $
 Total fees             : $
 Total net P&L          : $
 Capital deployed       : $
 Net expectancy /$1000  : $   <- net P&L per $1000 of capital deployed
 Avg hold               : s
```

## Fees

Per-contract trading fee, taken from the Kalshi fee schedule:

```
fee = round_up_to_next_cent( 0.07 × C × P × (1 − P) )
```

where `C` = contracts and `P` = fill price in dollars (0.01–0.99). The maximum
per-contract fee is at `P = $0.50` (`$0.0175 → $0.02`). Maker orders are charged
25% of the taker fee. The multiplier and maker fraction are configurable — **some
market categories use a multiplier above 0.07, so confirm the current KXBTCD rate
against the schedule before going live.**

Sources (verified July 2026):
- Kalshi Fee Schedule: <https://kalshi.com/docs/kalshi-fee-schedule.pdf>
- Kalshi Help Center — Fees: <https://help.kalshi.com/en/articles/13823805-fees>

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `KALSHI_DEMO` | `true` | Use demo API. **Must be `false` for live.** |
| `KALSHI_DRY_RUN` | `true` | Never send orders. **Must be `false` for live.** |
| `KALSHI_API_KEY_ID` | — | Kalshi API key id (access key). |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to the RSA private key PEM. |
| `KALSHI_PRIVATE_KEY_PEM` | — | PEM contents (alternative to the path). |
| `KALSHI_SERIES_TICKER` | `KXBTCD` | Series to trade. |
| `KALSHI_ENTRY_MIN_CENTS` | `85` | Entry band low (YES ask). |
| `KALSHI_ENTRY_MAX_CENTS` | `90` | Entry band high (YES ask). |
| `KALSHI_TARGET_NOTIONAL_USD` | `1000` | Target $ per entry. |
| `KALSHI_STOP_LOSS_CENTS` | `1` | Stop = `entry − this`. |
| `KALSHI_TAKE_PROFIT_CENTS` | `99` | Take-profit price. |
| `KALSHI_SCALE_OUT_CENTS` | `0` | Scale-out trigger price (0 disables). |
| `KALSHI_SCALE_OUT_FRACTION` | `0.5` | Fraction sold at scale-out. |
| `KALSHI_FEE_MULTIPLIER` | `0.07` | Fee formula multiplier. |
| `KALSHI_FEE_MAKER_FRACTION` | `0.25` | Maker fee as a fraction of taker. |
| `KALSHI_ASSUME_MAKER` | `false` | Price fills as maker in the fee calc. |
| `KALSHI_POLL_INTERVAL_SEC` | `5` | Manage-loop / default interval. |
| `KALSHI_MAX_RETRIES` | `6` | REST retry budget (429/5xx/connection). |
| `KALSHI_REQUEST_TIMEOUT_SEC` | `15` | Per-request timeout. |
| `KALSHI_DB_PATH` | `kalshi_bot.db` | SQLite trade log path. |
| `KALSHI_LOG_PATH` | `kalshi_bot.log` | Structured JSON log path (rotating). |

## Robustness

- **Position reconciliation:** the real net position is reconstructed from
  `GET /portfolio/fills` (buys netted against sells, size-weighted average
  entry) — the bot never assumes an order filled.
- **Rate limits:** `429` and `5xx` responses (and connection errors) are retried
  with exponential backoff + full jitter, honouring `Retry-After` when present.
- **Logging:** every event is written as one JSON object per line to a rotating
  file at `KALSHI_LOG_PATH`, plus a human-readable console line.

## Go-live checklist

> Trading real money on Kalshi carries real financial risk. Do this deliberately.

1. **Paper-trade first.** Run `--paper` for a meaningful sample and review the
   report. Confirm net expectancy per $1000 is positive *after* fees.
2. **Verify the fee multiplier** for `KXBTCD` against the current
   [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) and set
   `KALSHI_FEE_MULTIPLIER` accordingly (crypto categories may differ from 0.07).
3. **Credentials:** create an API key in your Kalshi account, store the private
   key PEM securely, and set `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`.
4. **Smoke-test signed reads on demo** (`KALSHI_DEMO=true`): run `--manage` and
   confirm `GET /portfolio/fills` reconciles and the WebSocket connects.
5. **Fund and confirm** the live account; understand max loss per hourly market.
6. **Flip DEMO off first, keep DRY_RUN on:** set `KALSHI_DEMO=false` and leave
   `KALSHI_DRY_RUN=true`. Confirm the bot resolves the *production* ticker and
   logs `DRY-RUN order (not sent)` at the moments it would trade.
7. **Go live:** set **both** `KALSHI_DEMO=false` **and** `KALSHI_DRY_RUN=false`.
   The startup banner will log `LIVE ORDERS ENABLED`. Start with a reduced
   `KALSHI_TARGET_NOTIONAL_USD` and watch the log file.
8. **Monitor** `kalshi_bot.log`, position reconciliation, and P&L for the first
   several cycles before stepping size back up.

> A real order is placed **only** when both `KALSHI_DEMO` and `KALSHI_DRY_RUN`
> are `false`. In every other configuration the bot logs the order it *would*
> have placed and sends nothing.

## Note on testing in this repository

The environment this module was authored in blocks outbound access to
`demo-api.kalshi.co` at the network-policy level, so the live-demo integration
steps above could not be exercised here. All non-network logic is covered by
`--selftest` (fees, sizing, entry/exit, order-book math, paper harness, WebSocket
message parsing, fills reconciliation, and RSA-PSS signing). Run the demo-API
steps from a network where Kalshi is reachable.
