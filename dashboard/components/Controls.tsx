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

export default function Controls({
  target, paused, updated,
}: { target: number; paused: boolean; updated: string | null }) {
  const router = useRouter();
  const [val, setVal] = useState(String(target));
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const dirty = Number(val) !== target && val.trim() !== "";

  async function saveTarget() {
    const n = Number(val);
    if (!isFinite(n) || n <= 0) { setMsg("Enter a positive dollar amount"); return; }
    setBusy("target"); setMsg(null);
    try { await put("target_notional_usd", n); setMsg("Saved — the bot picks it up within ~30s"); router.refresh(); }
    catch (e: any) { setMsg(e.message); }
    finally { setBusy(null); }
  }

  async function togglePause() {
    setBusy("pause"); setMsg(null);
    try { await put("entries_paused", !paused); router.refresh(); }
    catch (e: any) { setMsg(e.message); }
    finally { setBusy(null); }
  }

  return (
    <div className="card controls">
      <div className="controls-row">
        <div className="ctl">
          <label>Trade size (USD per entry)</label>
          <div className="input-row">
            <span className="prefix">$</span>
            <input
              type="number" min="0.01" step="0.5" value={val}
              onChange={(e) => setVal(e.target.value)}
              inputMode="decimal"
            />
            <button className="btn primary" onClick={saveTarget} disabled={busy !== null || !dirty}>
              {busy === "target" ? "Saving…" : "Save"}
            </button>
          </div>
          <div className="hint">Each entry buys ~this much. Keep it at or under your Kalshi balance.</div>
        </div>

        <div className="ctl">
          <label>New entries</label>
          <button
            className={`btn toggle ${paused ? "paused" : "running"}`}
            onClick={togglePause} disabled={busy !== null}
          >
            {busy === "pause" ? "…" : paused ? "▶ Resume entries" : "⏸ Pause entries"}
          </button>
          <div className="hint">
            {paused
              ? "Paused — no new positions. Open positions still exit normally."
              : "Running — the bot may open new positions on a valid signal."}
          </div>
        </div>
      </div>
      <div className="controls-foot">
        {msg && <span className="msg">{msg}</span>}
        {updated && <span className="upd">last change {new Date(updated).toLocaleString()}</span>}
      </div>
    </div>
  );
}
