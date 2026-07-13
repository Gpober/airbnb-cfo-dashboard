// Server-side data access for the Kalshi bot dashboard.
//
// Reads Supabase via PostgREST using the SERVICE-ROLE key. This module must
// only ever be imported from server components / server code -- the key is
// read from process.env and is never sent to the browser.

const BASE = process.env.SUPABASE_URL?.replace(/\/$/, "");
const KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

export const configured = Boolean(BASE && KEY);

async function rest<T = any>(path: string): Promise<T[]> {
  if (!configured) return [];
  try {
    const res = await fetch(`${BASE}/rest/v1/${path}`, {
      headers: { apikey: KEY as string, Authorization: `Bearer ${KEY}` },
      cache: "no-store",
    });
    if (!res.ok) return [];
    return (await res.json()) as T[];
  } catch {
    return [];
  }
}

// -- row shapes (subset of columns we render) ------------------------------- //

export type Run = {
  run_id: string;
  mode: "paper" | "demo" | "prod" | "live";
  series_ticker: string;
  config: Record<string, any> | null;
  started_at: string;
  ended_at: string | null;
};

export type Trade = {
  id: number;
  run_id: string;
  ticker: string;
  reason: "take_profit" | "stop_loss" | "scale_out" | "settled";
  contracts: number;
  entry_price: number;
  exit_price: number;
  entry_fee: number;
  exit_fee: number;
  gross_pnl: number;
  net_pnl: number;
  notional: number;
  hold_secs: number | null;
  opened_at: string | null;
  closed_at: string | null;
};

export type Tick = {
  ticker: string;
  yes_bid: number | null;
  yes_ask: number | null;
  source: "ws" | "rest" | null;
  observed_at: string;
};

export type AiDecision = {
  id: number;
  ticker: string;
  phase: "entry" | "manage";
  action: "enter" | "hold" | "exit" | "scale_out";
  confidence: number | null;
  reason: string | null;
  created_at: string;
};

export type Position = {
  ticker: string;
  net_contracts: number;
  avg_entry: number;
  reconciled_at: string;
};

// -- fetchers --------------------------------------------------------------- //

export async function getLatestRun(): Promise<Run | null> {
  const rows = await rest<Run>("kalshi_runs?select=*&order=started_at.desc&limit=1");
  return rows[0] ?? null;
}

export async function getTrades(limit = 500): Promise<Trade[]> {
  // closed_at ascending so the equity curve reads left-to-right in time.
  return rest<Trade>(
    `kalshi_trades?select=*&order=closed_at.asc.nullslast&limit=${limit}`,
  );
}

export async function getLatestTick(): Promise<Tick | null> {
  const rows = await rest<Tick>("kalshi_market_ticks?select=*&order=observed_at.desc&limit=1");
  return rows[0] ?? null;
}

export async function getLatestPosition(): Promise<Position | null> {
  const rows = await rest<Position>(
    "kalshi_positions?select=*&order=reconciled_at.desc&limit=1",
  );
  return rows[0] ?? null;
}

export async function getAiDecisions(limit = 15): Promise<AiDecision[]> {
  return rest<AiDecision>(
    `kalshi_ai_decisions?select=*&order=created_at.desc&limit=${limit}`,
  );
}

export type Settings = {
  target_notional_usd: number;
  entries_paused: boolean;
  entry_min_cents?: number;
  entry_max_cents?: number;
  updated?: string | null;
};

export async function getSettings(): Promise<Settings> {
  const rows = await rest<{ key: string; value: any; updated_at: string }>(
    "kalshi_settings?select=key,value,updated_at",
  );
  const map = new Map(rows.map((r) => [r.key, r]));
  const num = (k: string, d: number) => {
    const v = map.get(k)?.value;
    return typeof v === "number" ? v : Number(v ?? d);
  };
  const updated = rows.map((r) => r.updated_at).sort().at(-1) ?? null;
  return {
    target_notional_usd: num("target_notional_usd", 8),
    entries_paused: map.get("entries_paused")?.value === true,
    entry_min_cents: map.has("entry_min_cents") ? num("entry_min_cents", 85) : undefined,
    entry_max_cents: map.has("entry_max_cents") ? num("entry_max_cents", 90) : undefined,
    updated,
  };
}

// -- derived stats ---------------------------------------------------------- //

export type Stats = {
  trades: number;
  wins: number;
  winRate: number | null;
  netPnlCents: number;
  feesCents: number;
  expectancyPer1000: number | null; // dollars per $1000 staked
  avgHoldSecs: number | null;
  equity: { x: number; cum: number }[]; // cumulative net P&L (cents) by trade
};

export function computeStats(trades: Trade[]): Stats {
  const n = trades.length;
  if (n === 0) {
    return {
      trades: 0, wins: 0, winRate: null, netPnlCents: 0, feesCents: 0,
      expectancyPer1000: null, avgHoldSecs: null, equity: [],
    };
  }
  let wins = 0, net = 0, fees = 0, holdSum = 0, holdN = 0;
  const fracs: number[] = [];
  const equity: { x: number; cum: number }[] = [];
  let cum = 0;
  trades.forEach((t, i) => {
    const netP = t.net_pnl ?? 0;
    if (netP > 0) wins += 1;
    net += netP;
    fees += (t.entry_fee ?? 0) + (t.exit_fee ?? 0);
    if (t.notional) fracs.push(netP / t.notional);
    if (t.hold_secs != null) { holdSum += Number(t.hold_secs); holdN += 1; }
    cum += netP;
    equity.push({ x: i, cum });
  });
  const expectancyPer1000 =
    fracs.length ? (fracs.reduce((a, b) => a + b, 0) / fracs.length) * 1000 : null;
  return {
    trades: n,
    wins,
    winRate: wins / n,
    netPnlCents: net,
    feesCents: fees,
    expectancyPer1000,
    avgHoldSecs: holdN ? holdSum / holdN : null,
    equity,
  };
}

// -- helpers ---------------------------------------------------------------- //

export function modeLabel(run: Run | null): {
  text: string; live: boolean; tone: "live" | "safe" | "idle";
} {
  if (!run) return { text: "No runs yet", live: false, tone: "idle" };
  const c = run.config || {};
  const live = c.demo === false && c.dry_run === false;
  if (live) return { text: "LIVE — real money", live: true, tone: "live" };
  if (c.demo === false) return { text: "Prod data · dry-run (no orders)", live: false, tone: "safe" };
  if (run.mode === "paper") return { text: "Paper backtest", live: false, tone: "safe" };
  return { text: "Demo · dry-run", live: false, tone: "safe" };
}

export const dollars = (cents: number) =>
  `${cents < 0 ? "-" : ""}$${(Math.abs(cents) / 100).toFixed(2)}`;

export const pct = (x: number | null) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);
