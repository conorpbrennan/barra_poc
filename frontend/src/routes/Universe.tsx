// Estimation-universe lens (render_universe): index membership (Phase 1), filtration funnel
// (Phase 2), and span / high-confidence (Phase 3) — including the live fx×fy scatter of the
// estimation cloud vs the held book (Chris's VALUE/SIZE picture).
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { useUniverse, useFunnel, useSpan } from "../api/hooks";
import { LineChart } from "../components/LineChart";
import { QueryState } from "../components/ui";
import { pct, num } from "../lib/format";
import type { Rec, SpanResult } from "../api/types";

export function Universe() {
  const { date } = useApp();
  const [tab, setTab] = useState<"membership" | "funnel" | "span">("membership");

  return (
    <main className="lens">
      <h1>Estimation universe</h1>
      <p className="sub">Index membership · filtration funnel · factor-space span</p>
      <div className="row" style={{ marginBottom: "1rem" }}>
        {(["membership", "funnel", "span"] as const).map((t) => (
          <button key={t} className={tab === t ? "primary" : ""} onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      {tab === "membership" && <Membership date={date} />}
      {tab === "funnel" && <Funnel date={date} />}
      {tab === "span" && <Span date={date} />}
    </main>
  );
}

function Membership({ date }: { date: string }) {
  const q = useUniverse(date);
  return (
    <QueryState q={q}>
      {(d) => (
        <>
          <div className="hgroup" style={{ marginBottom: "1rem" }}>
            {d.buckets.map((b) => (
              <div className="hero" key={b}>
                <div className="v">{pct(d.latest.split[b] ?? 0)}</div>
                <div className="k">{b}</div>
              </div>
            ))}
          </div>
          <h2>Weight by bucket over filings</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "1.4rem 2rem" }}>
            {d.buckets.map((b) => (
              <LineChart key={b} title={b}
                points={d.series.map((r, i) => ({ x: i, y: typeof r[b] === "number" ? (r[b] as number) : null }))}
                fmt={(v) => pct(v)} />
            ))}
          </div>
          {d.detail.length > 0 && (
            <>
              <h2>Outside S&amp;P 1500 / unclassified ({d.latest.report_date})</h2>
              <table className="tufte" style={{ maxWidth: "48rem" }}>
                <thead><tr><th className="label">Issuer</th><th className="label">Ticker</th><th>Weight</th><th className="label">Bucket</th></tr></thead>
                <tbody>
                  {d.detail.map((r, i) => (
                    <tr key={i}>
                      <td className="label">{String(r.issuer ?? "")}</td>
                      <td className="label">{String(r.ticker ?? "")}</td>
                      <td>{pct(Number(r.weight))}</td>
                      <td className="label muted">{String(r.bucket ?? "")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </QueryState>
  );
}

function Funnel({ date }: { date: string }) {
  const q = useFunnel(date);
  return (
    <QueryState q={q}>
      {(d) => (
        <>
          <h2>Population → survivors ({d.selected_date})</h2>
          <table className="tufte" style={{ maxWidth: "40rem" }}>
            <tbody>
              <tr><td className="label">Population (PIT S&amp;P 500)</td><td>{num(Number(d.latest.population), 0)}</td></tr>
              {d.stages.map((s) => {
                const drop = Number((d.latest as Record<string, number>)[`drop:${s}`] ?? 0);
                return drop > 0 ? (
                  <tr key={s}><td className="label muted">− dropped at {s}</td><td className="muted">{drop}</td></tr>
                ) : null;
              })}
              <tr className="total"><td className="label">Survivors</td><td>{num(Number(d.latest.survivors), 0)}</td></tr>
              <tr><td className="label muted small">data unavailable (not counted)</td><td className="muted">{num(Number(d.latest.data_unavailable), 0)}</td></tr>
            </tbody>
          </table>
          {d.dropped.length > 0 && (
            <>
              <h2>Drop list</h2>
              <table className="tufte" style={{ maxWidth: "52rem" }}>
                <thead><tr><th className="label">Issuer</th><th className="label">Ticker</th><th className="label">Dropped at</th><th>Mcap</th><th>ADV</th></tr></thead>
                <tbody>
                  {d.dropped.slice(0, 30).map((r: Rec, i) => (
                    <tr key={i}>
                      <td className="label">{String(r.issuer ?? "")}</td>
                      <td className="label">{String(r.ticker ?? "")}</td>
                      <td className="label">{String(r.stage_dropped ?? "")}</td>
                      <td>{r.mcap != null ? num(Number(r.mcap) / 1e9, 1) + "B" : "—"}</td>
                      <td>{r.adv != null ? num(Number(r.adv) / 1e6, 1) + "M" : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <p className="muted small" style={{ maxWidth: "46rem", marginTop: "1rem" }}>{d.note}</p>
        </>
      )}
    </QueryState>
  );
}

function Span({ date }: { date: string }) {
  const [fx, setFx] = useState("Size");
  const [fy, setFy] = useState("ResidVol");
  const q = useSpan(date, fx, fy);
  return (
    <QueryState q={q}>
      {(d) => (
        <>
          <div className="hgroup" style={{ marginBottom: "1rem" }}>
            <div className="hero">
              <div className="v">{pct(Number(d.latest.inside_wt ?? 0))}</div>
              <div className="k">of book weight inside the estimation cloud</div>
            </div>
          </div>
          <h2>Inside-share over time</h2>
          <LineChart points={d.series.map((r, i) => ({ x: i, y: r.inside_wt }))} fmt={(v) => pct(v)}
            width={520} height={100} />

          <h2>Factor space — estimation cloud vs book</h2>
          <div className="row" style={{ marginBottom: "0.6rem" }}>
            <span className="muted small">x</span>
            <select value={fx} onChange={(e) => setFx(e.target.value)}>
              {d.factors.map((f) => <option key={f}>{f}</option>)}
            </select>
            <span className="muted small">y</span>
            <select value={fy} onChange={(e) => setFy(e.target.value)}>
              {d.factors.map((f) => <option key={f}>{f}</option>)}
            </select>
          </div>
          <Scatter scatter={d.scatter} />
          <p className="muted small" style={{ maxWidth: "46rem", marginTop: "1rem" }}>{d.note}</p>
        </>
      )}
    </QueryState>
  );
}

function Scatter({ scatter }: { scatter: SpanResult["scatter"] }) {
  const W = 420, H = 320, pad = 24;
  const all = [...scatter.cloud, ...scatter.held].filter((p) => p.x != null && p.y != null);
  if (all.length < 2) return <div className="muted small">insufficient points</div>;
  const xs = all.map((p) => p.x as number), ys = all.map((p) => p.y as number);
  const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
  const sx = (x: number) => pad + ((x - xmin) / (xmax - xmin || 1)) * (W - 2 * pad);
  const sy = (y: number) => H - pad - ((y - ymin) / (ymax - ymin || 1)) * (H - 2 * pad);
  return (
    <svg width={W} height={H} role="img" aria-label={`${scatter.fx} vs ${scatter.fy}`}
      style={{ border: "1px solid var(--line)" }}>
      {scatter.cloud.map((p, i) => p.x != null && p.y != null && (
        <circle key={`c${i}`} cx={sx(p.x)} cy={sy(p.y)} r={1.6} fill="#c9c6bd" />
      ))}
      {scatter.held.map((p, i) => p.x != null && p.y != null && (
        <circle key={`h${i}`} cx={sx(p.x)} cy={sy(p.y)} r={3}
          fill={p.inside ? "#3b5e8c" : "#a3322b"} fillOpacity={0.8}>
          <title>{p.issuer} {p.inside ? "(inside)" : "(outside)"}</title>
        </circle>
      ))}
      <text x={W - pad} y={H - 6} textAnchor="end" fontSize={11} fill="#6b6b63">{scatter.fx} →</text>
      <text x={6} y={pad} fontSize={11} fill="#6b6b63">{scatter.fy} ↑</text>
    </svg>
  );
}
