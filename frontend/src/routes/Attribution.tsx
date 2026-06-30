// Attribution lens. Surfaces the existing /attribution endpoint (risk attribution BY LEVEL —
// Country/Sector/Issuer/Position; risk_api.py:203). The Step-15 PnL feature is /pnl_attribution
// (renamed to avoid the clash — docs/vite-ui-plan.md §3); it is not yet shipped, so the PnL tab
// shows a placeholder until that endpoint lands.
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { useAttribution } from "../api/hooks";
import { LabelBar } from "../components/svg";
import { QueryState } from "../components/ui";
import { pct, num } from "../lib/format";

const LEVELS = ["sector", "issuer", "position", "country"];

export function Attribution() {
  const { date, scenario } = useApp();
  const [by, setBy] = useState("sector");
  const [tab, setTab] = useState<"risk" | "pnl">("risk");
  const q = useAttribution(date, scenario, by);

  return (
    <main className="lens">
      <h1>Attribution</h1>
      <p className="sub">Standalone risk by level · {scenario} · as-of {date}</p>

      <div className="row" style={{ marginBottom: "0.8rem" }}>
        <button className={tab === "risk" ? "primary" : ""} onClick={() => setTab("risk")}>Risk by level</button>
        <button className={tab === "pnl" ? "primary" : ""} onClick={() => setTab("pnl")}>PnL attribution</button>
      </div>

      {tab === "pnl" ? (
        <p className="muted small" style={{ maxWidth: "46rem" }}>
          PnL attribution (<code>/pnl_attribution</code>, Step 15) is not yet shipped on the API.
          The naming clash with the risk-attribution endpoint was resolved in the plan; this tab wires up
          once the endpoint lands.
        </p>
      ) : (
        <>
          <div className="row" style={{ marginBottom: "0.8rem" }}>
            <span className="muted small">Level</span>
            <select value={by} onChange={(e) => setBy(e.target.value)}>
              {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
          <QueryState q={q}>
            {(rows) => {
              const key = by === "position" ? "Position" : by[0].toUpperCase() + by.slice(1);
              const sorted = [...rows].sort(
                (a, b) => Number(b["Scenario VaR 99"] ?? 0) - Number(a["Scenario VaR 99"] ?? 0));
              const maxV = Math.max(...sorted.map((r) => Number(r["Scenario VaR 99"] ?? 0)), 1e-6);
              return (
                <div style={{ maxWidth: "52rem" }}>
                  {sorted.slice(0, 15).map((r, i) => (
                    <LabelBar key={i} label={String(r[by === "position" ? "Ticker" : key] ?? r[key] ?? "—")}
                      value={Number(r["Scenario VaR 99"] ?? 0)} max={maxV} suffix="" />
                  ))}
                  <table className="tufte" style={{ marginTop: "1rem" }}>
                    <thead><tr><th className="label">{key}</th><th>Net exposure</th><th>Scenario VaR 99</th><th>Worst loss</th></tr></thead>
                    <tbody>
                      {sorted.map((r, i) => (
                        <tr key={i}>
                          <td className="label">{String(r[by === "position" ? "Ticker" : key] ?? r[key] ?? "—")}</td>
                          <td>{num(Number(r["Net exposure"] ?? 0), 2)}</td>
                          <td>{pct(Number(r["Scenario VaR 99"] ?? 0))}</td>
                          <td>{pct(Number(r["Scenario worst loss"] ?? 0))}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            }}
          </QueryState>
        </>
      )}
    </main>
  );
}
