// Liquidity lens (render_liquidity): days-to-liquidate = MV / (participation·ADV). Participation /
// horizon sliders re-query the API. Hero share-within-horizon + weighted-avg days, worst names,
// and any no-ADV names reported separately (never counted instant).
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { useLiquidity } from "../api/hooks";
import { QueryState } from "../components/ui";
import { pct, days, compact } from "../lib/format";

export function Liquidity() {
  const { date, book } = useApp();
  const [participation, setParticipation] = useState(0.2);
  const [horizon, setHorizon] = useState(5);
  const q = useLiquidity(date, book, participation, horizon);

  return (
    <main className="lens">
      <h1>Liquidity</h1>
      <p className="sub">Days-to-liquidate the held book · as-of {date}</p>

      <div className="wrap" style={{ marginBottom: "1rem" }}>
        <label className="row">
          <span className="muted small">Participation {Math.round(participation * 100)}% of ADV</span>
          <input type="range" min={0.05} max={0.5} step={0.05} value={participation}
            onChange={(e) => setParticipation(Number(e.target.value))} />
        </label>
        <label className="row">
          <span className="muted small">Horizon {horizon}d</span>
          <input type="range" min={1} max={20} step={1} value={horizon}
            onChange={(e) => setHorizon(Number(e.target.value))} />
        </label>
      </div>

      <QueryState q={q}>
        {(d) => (
          <>
            <div className="hgroup" style={{ marginBottom: "1rem" }}>
              <div className="hero">
                <div className="v">{pct(d.pct_weight_within_horizon)}</div>
                <div className="k">of weight liquidatable ≤ {d.horizon_days}d</div>
              </div>
              <div className="hero">
                <div className="v">{days(d.weighted_avg_days)}</div>
                <div className="k">weighted-average days</div>
              </div>
              <div className="hero">
                <div className="v">{days(d.max_days)}</div>
                <div className="k">worst single name</div>
              </div>
              <div className="hero">
                <div className="v">{d.n_no_adv}</div>
                <div className="k">names with no ADV ({pct(d.weight_no_adv)} wt)</div>
              </div>
            </div>

            <h2>Least liquid names</h2>
            <table className="tufte" style={{ maxWidth: "52rem" }}>
              <thead>
                <tr>
                  <th className="label">Issuer</th><th className="label">Ticker</th><th className="label">Sector</th>
                  <th>Weight</th><th>MV</th><th>ADV</th><th>Days</th>
                </tr>
              </thead>
              <tbody>
                {d.detail.slice(0, 20).map((r, i) => (
                  <tr key={i}>
                    <td className="label">{String(r.Issuer ?? "")}</td>
                    <td className="label">{String(r.Ticker ?? "")}</td>
                    <td className="label muted">{String(r.Sector ?? "")}</td>
                    <td>{pct(Number(r.Weight))}</td>
                    <td>{compact(Number(r.MV))}</td>
                    <td>{compact(Number(r.ADV))}</td>
                    <td>{days(Number(r.days))}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {d.no_adv_names.length > 0 && (
              <>
                <h2>No ADV (reported separately)</h2>
                <div className="small muted">
                  {d.no_adv_names.map((n) => `${n.ticker ?? n.issuer} (${pct(Number(n.weight))})`).join(" · ")}
                </div>
              </>
            )}
            <p className="muted small" style={{ marginTop: "1rem", maxWidth: "46rem" }}>{d.note}</p>
          </>
        )}
      </QueryState>
    </main>
  );
}
