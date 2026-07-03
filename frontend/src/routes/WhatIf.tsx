// What-if lens (render_whatif): resize/drop held names or add a universe name, then recompute book
// risk before→after. /whatif with empty trades bootstraps the editor (holdings + universe + before
// figures); a run posts the modified weights and returns before/after/delta. "Before" matches the
// cube's reported figures exactly — only the delta is new information.
import { useMemo, useState } from "react";
import { useApp } from "../context/AppContext";
import { useWhatif, useHedge, useContributions } from "../api/hooks";
import { apiSend } from "../api/client";
import { QueryState, HowToRead } from "../components/ui";
import { pct, num, signedPct, signedNum } from "../lib/format";
import type { WhatIfResult } from "../api/types";

// ---- hedge panel: vol before/after neutralizing each factor, + the min-variance market h* ----
function HedgePanel({ date, book }: { date: string; book: string }) {
  const q = useHedge(date, book);
  return (
    <>
      <h2>Hedge — remove the risk you don&rsquo;t want</h2>
      <QueryState q={q}>
        {(h) => (
          <div style={{ maxWidth: "44rem" }}>
            <p className="muted small" style={{ margin: "0 0 0.4rem" }}>
              Book daily vol {pct(h.vol_base, 2)} (specific floor {pct(h.specific_vol, 2)} — no
              factor hedge touches it).
              {h.market_hedge && (
                <> Min-variance market hedge h* = {num(h.market_hedge.h_star, 2)} →
                  vol {pct(h.market_hedge.vol_after, 2)} (−{pct(h.market_hedge.vol_reduction, 2)}).</>
              )}
            </p>
            <table className="tufte">
              <thead><tr><th className="label">Neutralize</th><th>Exposure</th>
                <th>Hedge (pure-factor units)</th><th>Vol after</th><th>Vol saved</th></tr></thead>
              <tbody>
                {h.rows.slice(0, 8).map((r) => (
                  <tr key={r.factor}>
                    <td className="label">{r.factor}</td>
                    <td>{num(r.exposure, 2)}</td>
                    <td>{signedNum(r.hedge_units, 2)}</td>
                    <td>{pct(r.vol_after, 2)}</td>
                    <td>{r.vol_reduction > 1e-6 ? `−${pct(r.vol_reduction, 2)}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              Each row zeroes one net exposure with −x units of that pure factor portfolio
              (implementable in principle; purity costs leverage and turnover). The market h* is
              the single-instrument minimum-variance hedge, h* = −β.
            </p>
          </div>
        )}
      </QueryState>
    </>
  );
}

const RISK_ROWS: { key: keyof WhatIfResult["before"]; label: string; fmt: (v: number | null | undefined) => string }[] = [
  { key: "model_vol_1d", label: "Model vol (1d)", fmt: (v) => pct(v, 2) },
  { key: "scenario_var_99", label: "Scenario VaR 99", fmt: (v) => pct(v) },
  { key: "scenario_var_975", label: "Scenario VaR 97.5", fmt: (v) => pct(v) },
  { key: "es_975", label: "ES 97.5", fmt: (v) => pct(v) },
  { key: "es_99", label: "ES 99", fmt: (v) => pct(v) },
  { key: "specific_vol", label: "Specific vol", fmt: (v) => pct(v) },
  { key: "total_var_99", label: "Total VaR 99 (legacy)", fmt: (v) => pct(v) },
  { key: "top5_ctr_share", label: "Top-5 risk share", fmt: (v) => pct(v, 1) },
  { key: "gross", label: "Gross", fmt: (v) => num(v, 2) },
  { key: "net", label: "Net", fmt: (v) => num(v, 2) },
];

export function WhatIf() {
  const { date, book } = useApp();
  const boot = useWhatif(date, book, []);
  const contrib = useContributions(date, book);   // CTR ranking feeds the presets

  // edited weights keyed by position; undefined = unchanged
  const [edits, setEdits] = useState<Record<string, number>>({});
  const [addPos, setAddPos] = useState("");
  const [result, setResult] = useState<WhatIfResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const holdings = boot.data?.holdings ?? [];
  const universe = boot.data?.universe ?? [];
  const heldSet = useMemo(() => new Set(holdings.map((h) => h.position)), [holdings]);
  const addable = universe.filter((u) => !heldSet.has(u.position));

  const trades = useMemo(() => {
    const t: { position: string; weight: number }[] = [];
    for (const [position, weight] of Object.entries(edits)) t.push({ position, weight });
    return t;
  }, [edits]);

  // presets from the Euler ranking — allocate the trade to where the RISK is (CTR), not the
  // biggest weight; ch 09's "tracking error concentrates in few names" made actionable
  const presets = useMemo(() => {
    const ps = contrib.data?.positions ?? [];
    if (!ps.length) return [];
    const r5 = (v: number) => Math.round(v * 1e4) / 1e4;
    const top = ps[0];
    const top5 = ps.slice(0, 5);
    return [
      { label: `Drop ${top.ticker.toUpperCase()} (top risk name)`,
        note: `CTR ${(top.ctr * 100).toFixed(2)}% — the largest single contribution to book vol`,
        edits: { [top.position]: 0 } },
      { label: "Halve the top-5 risk names",
        note: `halves ${top5.map((p) => p.ticker.toUpperCase()).join(", ")} — `
          + `${((contrib.data?.sum_ctr ? top5.reduce((s, p) => s + p.ctr, 0) / contrib.data.sum_ctr : 0) * 100).toFixed(0)}% of book vol`,
        edits: Object.fromEntries(top5.map((p) => [p.position, r5(p.weight / 2)])) },
    ];
  }, [contrib.data]);

  async function run() {
    if (!trades.length) { setErr("edit at least one weight"); return; }
    setBusy(true); setErr(null);
    try {
      setResult(await apiSend<WhatIfResult>("POST", "/whatif", { date, book, trades }));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <main className="lens">
      <h1>Pre-trade what-if</h1>
      <p className="sub">Resize / drop / add names; recompute book risk before → after · as-of {date}</p>

      <HowToRead>
        Edits are <em>absolute target weights</em> (0.05 = 5% of book; 0 drops the name; names
        from the coverage universe can be added). The other weights are <em>not</em>
        renormalized — resizing one name changes gross and net, which is the point: you are
        trading, not rebasing. &ldquo;Before&rdquo; reproduces the cube&rsquo;s reported figures
        exactly, so <strong>only the before→after delta is new information</strong>. Model vol
        (1d) is the reference risk number; Scenario VaR/ES are the limit metrics; Total VaR 99
        is the legacy composite. The hedge panel below prices removing each factor with −x
        units of its pure factor portfolio (implementable in principle; purity costs leverage
        and turnover) and the min-variance market hedge h* = −β; no factor hedge touches the
        specific floor. Caveats: no transaction costs, one-day risk horizon, same full-history
        vols as everywhere else.
      </HowToRead>

      <QueryState q={boot}>
        {() => (
          <div style={{ display: "flex", gap: "2.5rem", flexWrap: "wrap", alignItems: "flex-start" }}>
            <div>
              <h2 style={{ marginTop: 0 }}>Holdings ({holdings.length})</h2>
              {presets.length > 0 && (
                <div className="row" style={{ flexWrap: "wrap", gap: "0.4rem", marginBottom: "0.6rem" }}>
                  <span className="muted small">Presets</span>
                  {presets.map((p) => (
                    <button key={p.label} title={p.note}
                      onClick={() => { setEdits(p.edits); setResult(null); setErr(null); }}>
                      {p.label}
                    </button>
                  ))}
                </div>
              )}
              <div className="row" style={{ marginBottom: "0.6rem" }}>
                <select value={addPos} onChange={(e) => setAddPos(e.target.value)}>
                  <option value="">add from universe…</option>
                  {addable.map((u) => <option key={u.position} value={u.position}>{u.ticker}</option>)}
                </select>
                <button disabled={!addPos} onClick={() => { if (addPos) { setEdits((s) => ({ ...s, [addPos]: 0.01 })); setAddPos(""); } }}>add</button>
              </div>
              <table className="tufte" style={{ maxWidth: "26rem" }}>
                <thead><tr><th className="label">Ticker</th><th>Current</th><th>New weight</th></tr></thead>
                <tbody>
                  {[...holdings, ...addable.filter((u) => u.position in edits).map((u) => ({ position: u.position, ticker: u.ticker, weight: 0 }))]
                    .map((h) => (
                      <tr key={h.position}>
                        <td className="label">{h.ticker}</td>
                        <td>{pct(h.weight)}</td>
                        <td>
                          <input type="number" step={0.005} style={{ width: "5rem" }}
                            value={edits[h.position] ?? ""}
                            placeholder={pct(h.weight)}
                            onChange={(e) => setEdits((s) => ({ ...s, [h.position]: Number(e.target.value) }))} />
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
              <div className="row" style={{ marginTop: "0.8rem" }}>
                <button className="primary" onClick={run} disabled={busy}>{busy ? "computing…" : "Run what-if"}</button>
                <button onClick={() => { setEdits({}); setResult(null); setErr(null); }}>Reset</button>
                {err && <span className="err small">{err}</span>}
              </div>
              <p className="muted small">Set a weight to 0 to drop a name. Weights are absolute targets.</p>
            </div>

            <div>
              <h2 style={{ marginTop: 0 }}>Risk before → after</h2>
              <table className="tufte" style={{ maxWidth: "30rem" }}>
                <thead><tr><th className="label">Measure</th><th>Before</th><th>After</th><th>Δ</th></tr></thead>
                <tbody>
                  {RISK_ROWS.map((r) => {
                    const before = result?.before[r.key] ?? boot.data?.before[r.key];
                    const after = result?.after[r.key];
                    const delta = result?.delta[r.key];
                    return (
                      <tr key={String(r.key)}>
                        <td className="label">{r.label}</td>
                        <td>{r.fmt(before as number)}</td>
                        <td>{after != null ? r.fmt(after as number) : "—"}</td>
                        <td className={delta != null && delta !== 0 ? (delta > 0 ? "rag-red" : "rag-green") : "muted"}>
                          {delta != null ? (r.key === "gross" || r.key === "net" ? signedNum(delta as number, 3) : signedPct(delta as number)) : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {result && result.trades.length > 0 && (
                <p className="muted small" style={{ maxWidth: "26rem" }}>
                  Applied: {result.trades.map((t) => `${t.ticker} ${pct(t.old)}→${pct(t.new)}`).join(", ")}
                </p>
              )}
            </div>
          </div>
        )}
      </QueryState>
      <HedgePanel date={date} book={book} />
    </main>
  );
}
