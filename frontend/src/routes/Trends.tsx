// Trends + Drawdown lens (render_trends + render_drawdown). Small multiples (shared time axis) of
// the book scenario measures over the calendar, the top style-factor exposures over time, and the
// constant-portfolio equity curve + underwater area for the global scenario set.
import { useApp } from "../context/AppContext";
import { useTrends, useDrawdown } from "../api/hooks";
import { LineChart } from "../components/LineChart";
import { LinePath } from "../components/svg";
import { QueryState } from "../components/ui";
import { pct, num } from "../lib/format";
import type { Rec } from "../api/types";

const STYLE_FACTORS = ["Size", "Value", "Momentum", "ResidVol", "Beta", "NonLinSize", "Growth", "Leverage"];

function col(recs: Rec[], xKey: string, yKey: string) {
  return recs.map((r, i) => ({
    x: typeof r[xKey] === "string" ? i : Number(r[xKey] ?? i),
    y: typeof r[yKey] === "number" ? (r[yKey] as number) : null,
  }));
}

export function Trends() {
  const { scenario } = useApp();
  const book = useTrends(scenario, "Scenario VaR 99,Scenario ES 97.5,Risk HHI,Total VaR 99,Specific vol");
  const byFactor = useTrends(scenario, "Net exposure", "Factor");

  return (
    <main className="lens">
      <h1>Trends</h1>
      <p className="sub">Book risk over 2016–2024 · scenario {scenario}</p>

      <QueryState q={book}>
        {(data) => {
          const r = data.records;
          const charts: { title: string; key: string; fmt: (v: number) => string }[] = [
            { title: "Total VaR 99", key: "Total VaR 99", fmt: (v) => pct(v) },
            { title: "Scenario VaR 99", key: "Scenario VaR 99", fmt: (v) => pct(v) },
            { title: "Scenario ES 97.5", key: "Scenario ES 97.5", fmt: (v) => pct(v) },
            { title: "Risk HHI", key: "Risk HHI", fmt: (v) => num(v, 3) },
            { title: "Specific vol", key: "Specific vol", fmt: (v) => pct(v) },
          ];
          return (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
              {charts.map((c) => (
                <LineChart key={c.key} title={c.title} points={col(r, "Date", c.key)} fmt={c.fmt} />
              ))}
            </div>
          );
        }}
      </QueryState>

      <h2>Top style-factor net exposure over time</h2>
      <QueryState q={byFactor}>
        {(data) => {
          // pivot tidy [Date, Factor, Net exposure] into per-factor series
          const byF = new Map<string, { x: number; y: number | null }[]>();
          const dates = Array.from(new Set(data.records.map((r) => String(r.Date)))).sort();
          const dix = new Map(dates.map((d, i) => [d, i]));
          for (const f of STYLE_FACTORS) byF.set(f, dates.map((_, i) => ({ x: i, y: null })));
          for (const rec of data.records) {
            const f = String(rec.Factor ?? "");
            if (!byF.has(f)) continue;
            const i = dix.get(String(rec.Date));
            if (i === undefined) continue;
            byF.get(f)![i] = { x: i, y: typeof rec["Net exposure"] === "number" ? (rec["Net exposure"] as number) : null };
          }
          const present = STYLE_FACTORS.filter((f) => (byF.get(f) ?? []).some((p) => p.y !== null));
          return (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
              {present.map((f) => (
                <LineChart key={f} title={f} points={byF.get(f)!} fmt={(v) => num(v, 2)} zero />
              ))}
            </div>
          );
        }}
      </QueryState>

      <Drawdown />
    </main>
  );
}

function Drawdown() {
  const { date, scenario, book } = useApp();
  const dd = useDrawdown(scenario, date, book);
  return (
    <>
      <h2>Drawdown (constant-portfolio, over the scenario path)</h2>
      <QueryState q={dd}>
        {(d) => {
          if (d.status !== "ok" || !d.path) {
            return <div className="muted small">insufficient path (hypothetical sets are length-1)</div>;
          }
          const W = 620, H = 120;
          const eq = d.path.map((p, i) => ({ x: i, y: p.equity }));
          const under = d.path.map((p, i) => ({ x: i, y: p.drawdown }));
          return (
            <div>
              <div className="wrap small" style={{ marginBottom: "0.5rem" }}>
                <span><span className="muted">max drawdown </span><b className="num rag-red">{pct(d.max_drawdown)}</b></span>
                <span><span className="muted">peak </span><span className="num">{d.peak_date}</span></span>
                <span><span className="muted">trough </span><span className="num">{d.trough_date}</span></span>
                <span><span className="muted">recovered </span><span className="num">{d.recovered ? d.recovery_date : "no"}</span></span>
                <span><span className="muted">longest underwater </span><span className="num">{d.longest_underwater_obs}d</span></span>
              </div>
              <svg width={W} height={H} role="img" aria-label="equity curve">
                <LinePath points={eq} width={W} height={H} color="#3b5e8c" />
              </svg>
              <svg width={W} height={56} role="img" aria-label="underwater">
                <LinePath points={under} width={W} height={56} color="#a3322b" fill="#a3322b22" yMax={0} />
              </svg>
              <div className="muted small">Equity curve (top) and underwater area (bottom). {d.set}.</div>
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
