// Drift lens (render_drift): the book's net factor exposure over time, the pre/post-split drift
// ranked, and each factor's drift decomposed into entered/exited/reweighted/loading_drift with a
// per-factor "lean" (rotation → benchmark vs re-pricing → hedge).
import { useState } from "react";
import { useDrift } from "../api/hooks";
import { LineChart } from "../components/LineChart";
import { QueryState } from "../components/ui";
import { signedNum, num } from "../lib/format";
import type { Rec } from "../api/types";

const STYLE = ["Size", "Value", "Momentum", "ResidVol", "Beta", "NonLinSize", "Growth", "Leverage"];

export function Drift() {
  const [split, setSplit] = useState("2021-01-01");
  const q = useDrift(split);

  return (
    <main className="lens">
      <h1>Style-drift attribution</h1>
      <p className="sub">Net factor exposure drift, entered/exited vs loading-drift</p>

      <label className="row" style={{ marginBottom: "1rem" }}>
        <span className="muted small">Pre/post split</span>
        <input type="text" value={split} onChange={(e) => setSplit(e.target.value)} style={{ width: "7rem" }} />
      </label>

      <QueryState q={q}>
        {(d) => {
          const present = STYLE.filter((f) => d.series.some((r) => typeof r[f] === "number"));
          return (
            <>
              <h2>Net exposure over time ({d.t0} → {d.t1})</h2>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
                {present.map((f) => (
                  <LineChart key={f} title={f}
                    points={d.series.map((r, i) => ({ x: i, y: typeof r[f] === "number" ? (r[f] as number) : null }))}
                    labels={d.series.map((r) => String(r.month ?? ""))}
                    fmt={(v) => num(v, 2)} zero />
                ))}
              </div>

              <h2>Drift attribution ({d.t0} → {d.t1})</h2>
              <table className="tufte" style={{ maxWidth: "62rem" }}>
                <thead>
                  <tr>
                    <th className="label">Factor</th><th>Early</th><th>Late</th><th>Δ</th>
                    <th>Entered</th><th>Exited</th><th>Reweighted</th><th>Loading drift</th>
                    <th className="label">Lean</th>
                  </tr>
                </thead>
                <tbody>
                  {d.summary.map((r: Rec, i) => (
                    <tr key={i}>
                      <td className="label">{String(r.factor)}</td>
                      <td>{num(Number(r.early), 2)}</td>
                      <td>{num(Number(r.late), 2)}</td>
                      <td><b>{signedNum(Number(r.delta), 2)}</b></td>
                      <td>{signedNum(Number(r.src_entered), 2)}</td>
                      <td>{signedNum(Number(r.src_exited), 2)}</td>
                      <td>{signedNum(Number(r.src_reweighted), 2)}</td>
                      <td>{signedNum(Number(r.src_loading_drift), 2)}</td>
                      <td className="label muted small">{String(r.lean ?? "").split(" — ")[0]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted small" style={{ maxWidth: "46rem", marginTop: "1rem" }}>{d.note}</p>
            </>
          );
        }}
      </QueryState>
    </main>
  );
}
