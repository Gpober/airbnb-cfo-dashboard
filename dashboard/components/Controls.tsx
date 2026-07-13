"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

async function put(key: string, value: number | boolean) {
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({})))?.error || "failed");
}

function NumField({
  label, hint, prefix, initial, settingKey, min, step, onSaved,
}: {
  label: string; hint: string; prefix?: string; initial: number;
  settingKey: string; min: string; step: string; onSaved: (m: string) => void;
}) {
  const [val, setVal] = useState(String(initial));
  const [busy, setBusy] = useState(false);
  const dirty = Number(val) !== initial && val.trim() !== "";

  async function save() {
    const n = Number(val);
    if (!isFinite(n)) return;
    setBusy(true);
    try { await put(settingKey, n); onSaved("Saved — the bot picks it up within ~30s"); }
    catch (e: any) { onSaved(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="ctl">
      <label>{label}</label>
      <div className="input-row">
        {prefix && <span className="prefix">{prefix}</span>}
        <input type="number" min={min} step={step} value={val} inputMode="decimal"
          onChange={(e) => setVal(e.target.value)} />
        <button className="btn primary" onClick={save} disabled={busy || !dirty}>
          {busy ? "…" : "Save"}
        </button>
      </div>
      <div className="hint">{hint}</div>
    </div>
  );
}

export default function Controls({
  target, paused, takeProfit, stopLoss, updated,
}: {
  target: number; paused: boolean; takeProfit: number; stopLoss: number;
  updated: string | null;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const done = (m: string) => { setMsg(m); router.refresh(); };

  async function togglePause() {
    setBusy(true); setMsg(null);
    try { await put("entries_paused", !paused); router.refresh(); }
    catch (e: any) { setMsg(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="card controls">
      <div className="controls-row">
        <NumField label="Trade size (USD per entry)" prefix="$" initial={target}
          settingKey="target_notional_usd" min="0.01" step="0.5" onSaved={done}
          hint="Each entry buys ~this much. Keep it at or under your Kalshi balance." />
        <div className="ctl">
          <label>New entries</label>
          <button className={`btn toggle ${paused ? "paused" : "running"}`}
            onClick={togglePause} disabled={busy}>
            {busy ? "…" : paused ? "▶ Resume entries" : "⏸ Pause entries"}
          </button>
          <div className="hint">
            {paused
              ? "Paused — no new positions. Open positions still exit normally."
              : "Running — the bot may open new positions on a valid signal."}
          </div>
        </div>
      </div>

      <div className="controls-row" style={{ marginTop: 18 }}>
        <NumField label="Take-profit (¢)" initial={takeProfit}
          settingKey="take_profit_cents" min="1" step="1" onSaved={done}
          hint="Sell when the bid reaches this. 99 = ride winners to near-settlement; lower (e.g. 95) banks gains sooner." />
        <NumField label="Stop-loss (¢ below entry)" initial={stopLoss}
          settingKey="stop_loss_cents" min="1" step="1" onSaved={done}
          hint="Sell if the bid falls this many cents below your entry. 1 = tight; higher tolerates more wobble." />
      </div>

      <div className="controls-foot">
        {msg && <span className="msg">{msg}</span>}
        {updated && <span className="upd">last change {new Date(updated).toLocaleString()}</span>}
      </div>
    </div>
  );
}
