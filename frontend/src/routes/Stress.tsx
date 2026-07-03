// Stress lens (render_stress): custom one-day shock (per-factor sigma → book P&L + contribution
// breakdown, POST /stress) and reverse stress (GET /reverse_stress — the single-factor move that
// breaches a target loss, ranked by vulnerability). Presets fill the sigma inputs: the cube's
// own Hypo:* definitions (served by /meta, one source), the primer's ch-09 worked example, and
// the reverse-stress weakest factor at its breach size.
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { useMeta, useReverseStress } from "../api/hooks";
import { apiSend } from "../api/client";
import { LabelBar } from "../components/svg";
import { QueryState, HowToRead } from "../components/ui";
import { pct, num, signedPct } from "../lib/format";
import type { StressResult } from "../api/types";

export function Stress() {
  const { date, book } = useApp();
  const { data: meta } = useMeta();
  const factors = (meta?.factors ?? []).filter((f) => f !== "Market");
  // presets: the cube's Hypo sets (verified to match /stress to float precision) + ch 09's example
  const rvq = useReverseStress(undefined, date, book);
  const presets: { label: string; shocks: Record<string, number>; note?: string }[] = [
    ...Object.entries(meta?.hypo_shocks ?? {}).map(([set, sh]) => ({
      label: set.replace("Hypo:", ""), shocks: sh,
      note: `the cube's ${set} scenario`,
    })),
    { label: "Value −2σ (primer ch 09)", shocks: { Value: -2 },
      note: "the It's Just Beta ch-09 worked example — compare naive vs conditional" },
    ...(rvq.data?.weakest?.sigma_to_breach != null ? [{
      label: `Weakest @ limit (${rvq.data.weakest.factor})`,
      shocks: { [rvq.data.weakest.factor]:
        Math.round(rvq.data.weakest.sigma_to_breach! * 10) / 10 },
      note: "the reverse-stress weakest factor at its breach-sized move",
    }] : []),
  ];

  const [shocks, setShocks] = useState<Record<string, number>>({});
  const [conditional, setConditional] = useState(true);
  const [result, setResult] = useState<StressResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    const active = Object.fromEntries(Object.entries(shocks).filter(([, v]) => v !== 0 && !Number.isNaN(v)));
    if (!Object.keys(active).length) { setErr("set at least one factor shock"); return; }
    setBusy(true); setErr(null);
    try {
      setResult(await apiSend<StressResult>("POST", "/stress",
        { shocks: active, date, book, conditional }));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <main className="lens">
      <h1>Stress test</h1>
      <p className="sub">Hypothetical one-day shocks · {book} · as-of {date}</p>

      <HowToRead>
        A shock is a per-factor move in σ units (that factor&rsquo;s own daily vol from the full
        2016–2024 history; −2 = a two-sigma down day). The <em>naive</em> loss is
        {" "}<code>ΔPnL = Σ x_k·(σ_k·vol_k)</code> — your shocked factors move, everything else
        held still; it matches the cube&rsquo;s Hypo:* sets to float precision. The
        {" "}<em>conditional</em> loss (keep it on — ch&nbsp;09: stress tests must use correlated
        shocks) propagates the shock through the factor covariance,
        {" "}<code>E[f|shock] = F·F_SS⁻¹·s</code>, so co-moving factors move too — on this book it
        is usually several times the naive number. Reverse stress inverts the question: the
        single-factor σ-move that produces a target loss, smallest move = most vulnerable.
        Caveats: linear (no convexity), one-day horizon, vols and correlations from the full
        history (a hot-regime factor reads too calm — check the vol ratio in the Model lens),
        and no shock touches specific risk.
      </HowToRead>

      <h2>Custom shock (σ per factor)</h2>
      <div className="row" style={{ flexWrap: "wrap", gap: "0.4rem", marginBottom: "0.6rem" }}>
        <span className="muted small">Presets</span>
        {presets.map((p) => (
          <button key={p.label} title={p.note}
            onClick={() => { setShocks(p.shocks); setResult(null); setErr(null); }}>
            {p.label}
          </button>
        ))}
      </div>
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
        <label className="row small muted" style={{ gap: "0.3rem" }}>
          <input type="checkbox" checked={conditional}
            onChange={(e) => setConditional(e.target.checked)} />
          correlated (conditional) — co-moving factors move too
        </label>
        {err && <span className="err small">{err}</span>}
      </div>

      {result && (
        <div style={{ maxWidth: "44rem" }}>
          <div className="row" style={{ gap: "2.5rem", marginBottom: "0.6rem" }}>
            <div className="hero">
              <div className="v rag-red">{pct(result.loss)}</div>
              <div className="k">naive loss — shocked factors only</div>
            </div>
            {result.conditional && (
              <div className="hero">
                <div className="v rag-red">{pct(result.conditional.loss)}</div>
                <div className="k">conditional loss — covariance propagated</div>
              </div>
            )}
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
          {result.conditional && (
            <>
              <h2>Conditional propagation</h2>
              <p className="muted small" style={{ margin: "0 0 0.4rem" }}>
                E[f | shock] — the factor covariance implies how every other factor moves when the
                shocked one does. A stress that holds them still understates a real event.
              </p>
              <table className="tufte">
                <thead><tr><th className="label">Factor</th><th>Exposure</th>
                  <th>Implied σ</th><th>Implied ret</th><th>P&L</th></tr></thead>
                <tbody>
                  {result.conditional.components
                    .filter((c) => c.shocked || Math.abs(c.pnl) > 1e-6)
                    .map((c) => (
                    <tr key={c.factor} style={c.shocked ? { fontWeight: 600 } : undefined}>
                      <td className="label">{c.factor}{c.shocked ? " ←" : ""}</td>
                      <td>{num(c.exposure, 2)}</td>
                      <td>{c.implied_sigma === null ? "—" : num(c.implied_sigma, 1)}</td>
                      <td>{signedPct(c.implied_return)}</td>
                      <td className={c.pnl < 0 ? "rag-red" : ""}>{signedPct(c.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
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
