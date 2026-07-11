# Kalshi BTC Bot — Dashboard

A read-only Next.js dashboard for the trading bot. It reads the bot's data from
Supabase (the same `kalshi_*` tables the bot writes to) and shows live status,
KPIs, an equity curve, recent trades, and the AI decision log.

It fetches Supabase **server-side with the service-role key**, so your data
stays private (RLS stays on, no public read policy needed) and the key is never
shipped to the browser.

## Deploy to Vercel

1. Push this repo to GitHub (already done).
2. In Vercel → **Add New… → Project** → import this repo.
3. **Important:** set **Root Directory** to `dashboard` (this is a subfolder of
   the repo; the bot lives at the repo root).
4. Framework preset: **Next.js** (auto-detected). Build command and output are
   the defaults — leave them.
5. Add two **Environment Variables** (same values the bot uses on Railway):

   | Name | Value |
   |------|-------|
   | `SUPABASE_URL` | `https://prdbqhvjqskfukttlxmy.supabase.co` |
   | `SUPABASE_SERVICE_ROLE_KEY` | your Supabase service-role key |

   These are **server-only** — they are read in server components and never
   exposed to the browser. Do **not** prefix them with `NEXT_PUBLIC_`.
6. Deploy. The dashboard auto-refreshes every 15s.

## Local development

```bash
cd dashboard
cp .env.example .env.local   # fill in your Supabase URL + service-role key
npm install
npm run dev                  # http://localhost:3000
```

## What it shows

- **Controls** — adjust **trade size ($/entry)** and **pause/resume new entries**
  live. Writes go through a server route (`/api/settings`) that uses the
  service-role key on the server and only accepts a whitelist of safe keys; the
  worker picks up changes within ~30s. The real-money gate (`DEMO`/`DRY_RUN`)
  is **not** adjustable here — it stays env-only on Railway.
- **Status pill** — whether the live market feed is fresh (a tick in the last 2 min).
- **Mode banner** — LIVE real-money vs. dry-run/demo/paper, plus the current
  market's bid/ask and any open position.
- **KPIs** — trade count, win rate, net P&L, net fees, expectancy per $1,000, avg hold.
- **Equity curve** — cumulative net P&L across closed trades.
- **Recent trades** — the last 25 closes with exit reason and P&L.
- **AI decisions** — the optional AI layer's proposals (only present if enabled).

Until the bot places its first trade the tables show a friendly empty state —
that's expected while the hourly book is thin (weekends / overnight).

## Security notes

- The service-role key bypasses RLS; keep it only in Vercel's env vars.
- This app performs **reads only** — it never writes to Supabase or touches Kalshi.
