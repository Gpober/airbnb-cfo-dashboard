import AutoRefresh from "@/components/AutoRefresh";
import {
  configured, getLatestRun, getTrades, getLatestTick, getLatestPosition,
  getAiDecisions, computeStats, modeLabel, dollars, pct, type Trade,
} from "@/lib/data";

export const dynamic = "force-dynamic"; // always render fresh from Supabase

function EquityCurve({ points }: { points: { x: number; cum: number }[] }) {
  if (points.length < 2) {
    return <div className="empty">Equity curve appears once there are 2+ closed trades.</div>;
  }
  const W = 1040, H = 220, pad = 10;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.cum);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(0, ...ys), maxY = Math.max(0, ...ys);
  const sx = (x: number) => pad + ((x - minX) / (maxX - minX || 1)) * (W - 2 * pad);
  const sy = (y: number) => H - pad - ((y - minY) / (maxY - minY || 1)) * (H - 2 * pad);
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p.x).toFixed(1)},${sy(p.cum).toFixed(1)}`).join(" ");
  const last = ys[ys.length - 1];
  const stroke = last >= 0 ? "var(--green)" : "var(--red)";
  const zeroY = sy(0);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
      <line x1={pad} y1={zeroY} x2={W - pad} y2={zeroY} stroke="var(--border)" strokeDasharray="4 4" />
      <path d={`${d} L${sx(maxX)},${zeroY} L${sx(minX)},${zeroY} Z`} fill={stroke} opacity="0.08" />
      <path d={d} fill="none" stroke={stroke} strokeWidth="2" />
    </svg>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "pos" | "neg" }) {
  return (
    <div className="card kpi">
      <div className="label">{label}</div>
      <div className={`value ${tone || ""}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

const ago = (iso: string | null) => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

export default async function Page() {
  if (!configured) {
    return (
      <div className="wrap">
        <div className="title">Kalshi BTC Bot</div>
        <div className="empty" style={{ marginTop: 20 }}>
          <div className="big">Not connected to Supabase yet</div>
          Set <code>SUPABASE_URL</code> and <code>SUPABASE_SERVICE_ROLE_KEY</code> in your Vercel
          project settings, then redeploy. See <code>dashboard/README.md</code>.
        </div>
      </div>
    );
  }

  const [run, trades, tick, position, ai] = await Promise.all([
    getLatestRun(), getTrades(), getLatestTick(), getLatestPosition(), getAiDecisions(),
  ]);
  const stats = computeStats(trades);
  const mode = modeLabel(run);
  const feedLive = tick && (Date.now() - new Date(tick.observed_at).getTime()) < 120_000;
  const recent = [...trades].reverse().slice(0, 25);

  return (
    <div className="wrap">
      <AutoRefresh seconds={15} />

      <div className="header">
        <div>
          <div className="title">Kalshi BTC Bot</div>
          <div className="subtitle">
            {run ? `${run.series_ticker} · run ${run.run_id}` : "hourly BTC above/below (KXBTCD)"}
          </div>
        </div>
        <div className={`pill ${feedLive ? "safe" : "idle"}`}>
          <span className="dot" />
          {feedLive ? "Feed live" : "Feed idle"}
          {tick && ` · ${ago(tick.observed_at)}`}
        </div>
      </div>

      <div className={`banner ${mode.tone === "live" ? "live" : ""}`}>
        <span className={`pill ${mode.tone}`}><span className="dot" />{mode.text}</span>
        <span style={{ color: "var(--muted)" }}>
          {tick ? `${tick.ticker} — bid ${tick.yes_bid ?? "—"}¢ / ask ${tick.yes_ask ?? "—"}¢` : "waiting for first market tick"}
          {position && position.net_contracts !== 0 &&
            ` · open: ${position.net_contracts} @ ${position.avg_entry}¢`}
        </span>
      </div>

      <div className="grid">
        <Kpi label="Trades" value={String(stats.trades)} sub={stats.trades ? `${stats.wins} wins` : "none yet"} />
        <Kpi label="Win rate" value={pct(stats.winRate)} />
        <Kpi
          label="Net P&L"
          value={dollars(stats.netPnlCents)}
          tone={stats.netPnlCents >= 0 ? "pos" : "neg"}
          sub={`fees ${dollars(stats.feesCents)}`}
        />
        <Kpi
          label="Expectancy"
          value={stats.expectancyPer1000 == null ? "—" : `${stats.expectancyPer1000 >= 0 ? "+" : ""}$${stats.expectancyPer1000.toFixed(2)}`}
          sub="per $1,000 staked"
          tone={stats.expectancyPer1000 != null ? (stats.expectancyPer1000 >= 0 ? "pos" : "neg") : undefined}
        />
        <Kpi
          label="Avg hold"
          value={stats.avgHoldSecs == null ? "—" : stats.avgHoldSecs < 90 ? `${Math.round(stats.avgHoldSecs)}s` : `${Math.round(stats.avgHoldSecs / 60)}m`}
        />
      </div>

      <div className="section-title">Equity curve (cumulative net P&L)</div>
      <div className="card"><EquityCurve points={stats.equity} /></div>

      <div className="section-title">Recent trades</div>
      {recent.length === 0 ? (
        <div className="empty">
          <div className="big">No trades yet</div>
          The bot is armed and watching. Trades appear here the moment it fills its first order —
          most likely a weekday during US market hours when the hourly book has liquidity.
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <table className="mono">
            <thead>
              <tr>
                <th>Closed</th><th>Ticker</th><th>Exit</th><th>Qty</th>
                <th>Entry ¢</th><th>Exit ¢</th><th>Fees</th><th>Net P&L</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((t: Trade) => {
                const tagCls = t.reason === "take_profit" ? "tp" : t.reason === "stop_loss" ? "sl" : "so";
                return (
                  <tr key={t.id}>
                    <td>{ago(t.closed_at)}</td>
                    <td>{t.ticker}</td>
                    <td><span className={`tag ${tagCls}`}>{t.reason.replace("_", " ")}</span></td>
                    <td>{t.contracts}</td>
                    <td>{t.entry_price}</td>
                    <td>{t.exit_price}</td>
                    <td>{dollars((t.entry_fee ?? 0) + (t.exit_fee ?? 0))}</td>
                    <td className={t.net_pnl >= 0 ? "pos" : "neg"}>{dollars(t.net_pnl)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {ai.length > 0 && (
        <>
          <div className="section-title">AI decisions</div>
          <div className="card" style={{ padding: 0, overflowX: "auto" }}>
            <table>
              <thead>
                <tr><th>When</th><th>Phase</th><th>Action</th><th>Conf.</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {ai.map((d) => (
                  <tr key={d.id}>
                    <td className="mono">{ago(d.created_at)}</td>
                    <td>{d.phase}</td>
                    <td><span className={`tag ${d.action}`}>{d.action}</span></td>
                    <td className="mono">{d.confidence == null ? "—" : d.confidence.toFixed(2)}</td>
                    <td style={{ textAlign: "left", color: "var(--muted)" }}>{d.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="footer">
        Auto-refreshes every 15s · read-only · {run ? `started ${ago(run.started_at)}` : ""}
      </div>
    </div>
  );
}
