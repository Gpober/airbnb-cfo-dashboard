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
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to the RSA private key PEM (local/dev). |
| `KALSHI_PRIVATE_KEY_PEM` | — | PEM contents (bot auto-repairs mangled newlines). |
| `KALSHI_PRIVATE_KEY_B64` | — | **base64 of the PEM** — one unmanglable line. Preferred on Railway/Render/Fly. |
| `KALSHI_SERIES_TICKER` | `KXBTCD` | Series to trade. |
| `KALSHI_API_BASE` | — | Override the REST base URL (prod default `api.elections.kalshi.com`). |
| `KALSHI_WS_BASE` | — | Override the WebSocket base URL. |
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
| `KALSHI_ROLLOVER_CHECK_SEC` | `60` | How often `--forever` re-checks the active hourly ticker. |
| `KALSHI_MAX_RETRIES` | `6` | REST retry budget (429/5xx/connection). |
| `KALSHI_REQUEST_TIMEOUT_SEC` | `15` | Per-request timeout. |
| `KALSHI_DB_PATH` | `kalshi_bot.db` | SQLite trade log path. |
| `KALSHI_LOG_PATH` | `kalshi_bot.log` | Structured JSON log path (rotating). |
| `SUPABASE_URL` | — | Supabase project URL. Enables the Supabase mirror when set with the key below. |
| `SUPABASE_SERVICE_ROLE_KEY` | — | Supabase **service-role** key (server-side only). Writes bypass RLS. |
| `KALSHI_AI_ENABLED` | `false` | Turn the optional AI decision layer on. |
| `KALSHI_AI_AUTHORITY` | `advisory` | `advisory` (AI may veto entries) or `decider` (veto + size down). |
| `KALSHI_AI_MODEL` | `claude-opus-4-8` | Anthropic model id for the decision layer. |
| `KALSHI_AI_MIN_INTERVAL_SEC` | `30` | Min seconds between AI exit checks (cost control). |
| `ANTHROPIC_API_KEY` | — | Anthropic API key. Required only if `KALSHI_AI_ENABLED=true`. |

> **On a hosting platform, prefer `KALSHI_PRIVATE_KEY_B64`.** Env-var editors
> often collapse a multiline PEM's newlines (or turn them into spaces / literal
> `\n`), which corrupts the key. base64 is a single line with no special
> characters, so it survives intact:
>
> ```bash
> base64 -w0 kalshi_private_key.pem   # Linux
> base64 -i kalshi_private_key.pem    # macOS
> ```
>
> Paste the output as `KALSHI_PRIVATE_KEY_B64`. (The bot also auto-repairs a
> mangled `KALSHI_PRIVATE_KEY_PEM`, but base64 avoids the problem entirely.)

## Deployment (always-on worker + dashboard)

The strategy needs a process that stays up 24/7 (constant BTC volatility, tight
stop-loss, live WebSocket). **Vercel is serverless and cannot host that** — it
has no persistent process. The clean split:

| Piece | Host | Env vars it needs |
|---|---|---|
| **Bot** (`--forever`) | a persistent worker — **Railway** / Render / Fly.io | `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PEM`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (+ `KALSHI_DEMO`/`KALSHI_DRY_RUN`) |
| **Dashboard** (optional) | **Vercel** | `NEXT_PUBLIC_SUPABASE_URL` only (read Supabase server-side with the service-role key) |

The `--forever` worker runs the manage loop indefinitely and **follows the
hourly rollover**: it re-resolves the active `KXBTCD` market every
`KALSHI_ROLLOVER_CHECK_SEC` and reconnects the WebSocket to the new contract.

**Deploy (Railway example):**
1. Push this repo to GitHub (done).
2. Railway → New Project → Deploy from GitHub repo → pick this repo. It builds
   the included `Dockerfile` automatically (which runs `--selftest` at build
   time and starts `--forever`).
3. Railway → Variables → add the worker env vars from the table above. Keep
   `KALSHI_DEMO=true` and `KALSHI_DRY_RUN=true` until you've validated.
4. Deploy. Watch the logs for `resolved active ticker` and `reconciled …`.

`Dockerfile`, `Procfile`, and `railway.json` are included; Render and Fly.io
consume the same `Dockerfile` (`render.yaml`/`fly.toml` not included but the
image is identical).

### Supabase mirror

When `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are set, every run, trade,
order, position, and tick is written to the `kalshi_*` tables (best-effort — a
Supabase outage never interrupts trading; local SQLite stays the durable log).
Leave them unset and the bot runs exactly as before with no external writes.

## Robustness

- **Position reconciliation:** the real net position is reconstructed from
  `GET /portfolio/fills` (buys netted against sells, size-weighted average
  entry) — the bot never assumes an order filled.
- **Rate limits:** `429` and `5xx` responses (and connection errors) are retried
  with exponential backoff + full jitter, honouring `Retry-After` when present.
- **Logging:** every event is written as one JSON object per line to a rotating
  file at `KALSHI_LOG_PATH`, plus a human-readable console line.

## AI decision layer (optional, off by default)

`ai_strategy.py` adds an optional AI layer (Claude Opus 4.8) that reviews each
setup and can make the bot **more** conservative — never looser. Design rule:
**the AI proposes, deterministic code disposes.**

- It can **veto** a marginal entry, **size an entry down** (`decider` authority),
  or trigger an **earlier exit** than the stop/take-profit.
- It **cannot** place orders, enter outside the 85–90¢ band, size up, or bypass
  the stop-loss or the `DEMO`/`DRY_RUN` gate. The deterministic stop-loss and
  take-profit always fire regardless of the AI.
- Everything degrades gracefully: no `ANTHROPIC_API_KEY`, package missing, API
  error, or bad response → "no AI opinion" → the bot runs on its rules.
- Every proposal is logged to the `kalshi_ai_decisions` Supabase table (what the
  AI saw + what it proposed) for audit and backtesting.

**You don't need Anthropic to run the bot** — the AI layer is off unless you set
`KALSHI_AI_ENABLED=true` *and* provide `ANTHROPIC_API_KEY`. These go on the
**bot host (Railway)**, never on Vercel (the dashboard doesn't call Claude).

**Prove it before trusting it:** run `--paper` with the AI enabled and compare
net expectancy per $1000 against the rule-based baseline. It only earns live
trading if it beats the baseline *after fees* — and even then, behind the same
guardrails.

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
