// Trends lens (render_trends). Small multiples (shared time axis) of the book scenario measures
// over the calendar and the top style-factor exposures over time. The Drawdown panel and the
// Risk-HHI chart were removed 2026-07-02 (itsjustbeta scope audit — neither is a primer report;
// concentration now reads as Top-5 risk share on the Overview / What-if).
import { useApp } from "../context/AppContext";
import { useTrends } from "../api/hooks";
import { LineChart } from "../components/LineChart";
import { StreamPanel } from "../components/StreamPanel";
import { QueryState } from "../components/ui";
import { pct, num } from "../lib/format";
import type { Rec } from "../api/types";

const STYLE_FACTORS = ["Size", "Value", "Momentum", "ResidVol", "Beta", "NonLinSize", "MegaCap", "RateBeta", "NdxBeta", "Leverage"];

function col(recs: Rec[], xKey: string, yKey: string) {
  return recs.map((r, i) => ({
    x: typeof r[xKey] === "string" ? i : Number(r[xKey] ?? i),
    y: typeof r[yKey] === "number" ? (r[yKey] as number) : null,
  }));
}

export function Trends() {
  const { scenario } = useApp();
  const book = useTrends(scenario, "Model vol,Scenario VaR 99,Scenario ES 97.5,Total VaR 99,Specific vol");
  const byFactor = useTrends(scenario, "Net exposure", "Factor");

  return (
    <main className="lens">
      <h1>Trends</h1>
      <p className="sub">Book risk over the full calendar · scenario {scenario}</p>

      <QueryState q={book}>
        {(data) => {
          const r = data.records;
          const dates = r.map((rec) => String(rec.Date ?? "").slice(0, 10));
          const charts: { title: string; key: string; fmt: (v: number) => string }[] = [
            { title: "Model vol (the reference)", key: "Model vol", fmt: (v) => pct(v, 2) },
            { title: "Scenario VaR 99", key: "Scenario VaR 99", fmt: (v) => pct(v) },
            { title: "Scenario ES 97.5", key: "Scenario ES 97.5", fmt: (v) => pct(v) },
            { title: "Specific vol", key: "Specific vol", fmt: (v) => pct(v) },
            { title: "Total VaR 99 (legacy)", key: "Total VaR 99", fmt: (v) => pct(v) },
          ];
          return (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
              {charts.map((c) => (
                <LineChart key={c.key} title={c.title} points={col(r, "Date", c.key)}
                  labels={dates} fmt={c.fmt} />
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
          const dlabels = dates.map((d) => d.slice(0, 10));
          return (
            <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
              {present.map((f) => (
                <LineChart key={f} title={f} points={byF.get(f)!} labels={dlabels}
                  fmt={(v) => num(v, 2)} zero />
              ))}
            </div>
          );
        }}
      </QueryState>

      <hr className="rule" />

      {/* ---- trends commentary in the desk risk-manager voice (CHRIS_VOICE) ---- */}
      <div style={{ maxWidth: "46rem" }}>
        <h2>Risk-manager read</h2>
        <StreamPanel path="/trends/analysis"
          body={{ set: scenario }}
          cacheKey={`trends:${scenario}`}
          label="Generate trends read" />
      </div>
    </main>
  );
}
