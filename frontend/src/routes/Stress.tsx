// Stress lens (render_stress): custom one-day shock (per-factor sigma → book P&L + contribution
// breakdown, POST /stress) and reverse stress (GET /reverse_stress — the single-factor move that
// breaches a target loss, ranked by vulnerability).
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { useMeta, useReverseStress } from "../api/hooks";
import { apiSend } from "../api/client";
import { LabelBar } from "../components/svg";
import { QueryState } from "../components/ui";
import { pct, num, signedPct } from "../lib/format";
import type { StressResult } from "../api/types";

export function Stress() {
  const { date, book } = useApp();
  const { data: meta } = useMeta();
  const factors = (meta?.factors ?? []).filter((f) => f !== "Market");

  const [shocks, setShocks] = useState<Record<string, number>>({});
  const [result, setResult] = useState<StressResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    const active = Object.fromEntries(Object.entries(shocks).filter(([, v]) => v !== 0 && !Number.isNaN(v)));
    if (!Object.keys(active).length) { setErr("set at least one factor shock"); return; }
    setBusy(true); setErr(null);
    try {
      setResult(await apiSend<StressResult>("POST", "/stress", { shocks: active, date, book }));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <main className="lens">
      <h1>Stress test</h1>
      <p className="sub">Hypothetical one-day shocks · {book} · as-of {date}</p>

      <h2>Custom shock (σ per factor)</h2>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem 1.4rem", maxWidth: "44rem" }}>
        {factors.map((f) => (
          <label key={f} className="row" style={{ width: "12rem", justifyContent: "space-between" }}>
            <span className="muted small">{f}</span>
            <input type="number" step={0.5} style={{ width: "4.5rem" }}
              value={shocks[f] ?? ""} placeholder="0"
              onChange={(e) => setShocks((s) => ({ ...s, [f]: Number(e.target.value) }))} />
          </label>
        ))}
      </div>
      <div className="row" style={{ margin: "0.8rem 0" }}>
        <button className="primary" onClick={run} disabled={busy}>{busy ? "computing…" : "Run stress"}</button>
        <button onClick={() => { setShocks({}); setResult(null); setErr(null); }}>Clear</button>
        {err && <span className="err small">{err}</span>}
      </div>

      {result && (
        <div style={{ maxWidth: "44rem" }}>
          <div className="hero" style={{ marginBottom: "0.6rem" }}>
            <div className="v rag-red">{pct(result.loss)}</div>
            <div className="k">book loss (P&L {signedPct(result.total_pnl)})</div>
          </div>
          <table className="tufte">
            <thead><tr><th className="label">Factor</th><th>Exposure</th><th>σ</th><th>Vol</th><th>Shock ret</th><th>P&L</th></tr></thead>
            <tbody>
              {result.components.map((c) => (
                <tr key={c.factor}>
                  <td className="label">{c.factor}</td>
                  <td>{num(c.exposure, 2)}</td>
                  <td>{num(c.sigma, 1)}</td>
                  <td>{pct(c.vol)}</td>
                  <td>{signedPct(c.shock_return)}</td>
                  <td className={c.pnl < 0 ? "rag-red" : ""}>{signedPct(c.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Reverse date={date} book={book} />
    </main>
  );
}

function Reverse({ date, book }: { date: string; book: string }) {
  const [loss, setLoss] = useState<number | undefined>(undefined);
  const q = useReverseStress(loss, date, book);
  return (
    <>
      <h2>Reverse stress — most vulnerable factor</h2>
      <label className="row" style={{ marginBottom: "0.6rem" }}>
        <span className="muted small">Target loss</span>
        <input type="number" step={0.01} placeholder="VaR limit" style={{ width: "5rem" }}
          value={loss ?? ""} onChange={(e) => setLoss(e.target.value ? Number(e.target.value) : undefined)} />
      </label>
      <QueryState q={q}>
        {(d) => {
          const maxAbs = Math.max(...d.factors.map((f) => f.abs_sigma ?? 0), 1);
          return (
            <div style={{ maxWidth: "40rem" }}>
              <p className="small muted">
                Target loss {pct(d.loss)}. Smallest σ-move = most vulnerable
                {d.weakest ? `: ${d.weakest.factor} at ${num(d.weakest.sigma_to_breach, 1)}σ` : ""}.
              </p>
              {d.factors.slice(0, 10).map((f) => (
                <LabelBar key={f.factor} label={f.factor} value={f.abs_sigma ?? 0} max={maxAbs}
                  suffix="σ" />
              ))}
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
