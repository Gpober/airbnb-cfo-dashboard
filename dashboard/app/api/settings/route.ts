import { NextResponse } from "next/server";

// Writes a single bot control to Supabase (kalshi_settings) using the
// service-role key on the SERVER only. A strict whitelist mirrors the bot's:
// demo/dry_run and anything else can never be set from here.

const BASE = process.env.SUPABASE_URL?.replace(/\/$/, "");
const KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

const NUMERIC = new Set([
  "target_notional_usd", "entry_min_cents", "entry_max_cents",
  "stop_loss_cents", "take_profit_cents",
]);
const BOOLEAN = new Set(["entries_paused"]);

function clampCents(n: number) {
  return Math.max(1, Math.min(99, Math.round(n)));
}

export async function POST(req: Request) {
  if (!BASE || !KEY) {
    return NextResponse.json({ error: "not configured" }, { status: 500 });
  }
  const body = await req.json().catch(() => null);
  const key = body?.key;
  if (typeof key !== "string" || (!NUMERIC.has(key) && !BOOLEAN.has(key))) {
    return NextResponse.json({ error: "unknown or forbidden key" }, { status: 400 });
  }

  let value: number | boolean;
  if (BOOLEAN.has(key)) {
    value = Boolean(body.value);
  } else {
    const n = Number(body.value);
    if (!isFinite(n)) {
      return NextResponse.json({ error: "value must be a number" }, { status: 400 });
    }
    if (key === "target_notional_usd") {
      value = Math.min(100000, Math.max(0.01, n)); // positive, capped
    } else if (key === "take_profit_cents") {
      value = Math.max(1, Math.min(100, Math.round(n)));
    } else {
      value = clampCents(n);
    }
  }

  const res = await fetch(`${BASE}/rest/v1/kalshi_settings?on_conflict=key`, {
    method: "POST",
    headers: {
      apikey: KEY,
      Authorization: `Bearer ${KEY}`,
      "Content-Type": "application/json",
      Prefer: "resolution=merge-duplicates,return=minimal",
    },
    body: JSON.stringify({
      key,
      value,
      updated_at: new Date().toISOString(),
      updated_by: "dashboard",
    }),
  });

  if (!res.ok) {
    return NextResponse.json({ error: await res.text() }, { status: 502 });
  }
  return NextResponse.json({ ok: true, key, value });
}
