// Checks lens: the detail behind the Overview RAG strip — desk limits (bullet graphs + table),
// data-quality report (render_dq_badge), and the VaR backtest (render_backtest_badge).
import { useApp } from "../context/AppContext";
import { useLimits, useDq, useBacktest } from "../api/hooks";
import { BulletGraph } from "../components/svg";
import { QueryState, RagDot } from "../components/ui";
import { pct, num, ragLabel } from "../lib/format";

export function Checks() {
  const { date, scenario, book } = useApp();
  const limits = useLimits(date, scenario, book);
  const dq = useDq();
  const bt = useBacktest("HistFull", date, book);

  return (
    <main className="lens">
      <h1>Checks</h1>
      <p className="sub">Desk limits · data quality · VaR backtest · as-of {date}</p>

      <h2>Desk limits — {scenario}</h2>
      <QueryState q={limits}>
        {(d) =>
          d.configured ? (
            <table className="tufte" style={{ maxWidth: "46rem" }}>
              <thead>
                <tr>
                  <th className="label">Limit</th><th className="label">Detail</th>
                  <th>Value</th><th>Warn</th><th>Limit</th><th></th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {d.checks.map((c) => (
                  <tr key={c.name}>
                    <td className="label">{c.name}</td>
                    <td className="label muted small">{c.detail ?? ""}</td>
                    <td>{pct(c.value)}</td>
                    <td className="muted">{pct(c.warn)}</td>
                    <td className="muted">{pct(c.limit)}</td>
                    <td><BulletGraph value={c.value} warn={c.warn} limit={c.limit} status={c.status} width={120} /></td>
                    <td><RagDot status={c.status} /> {ragLabel(c.status)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <div className="muted small">no limits configured</div>
        }
      </QueryState>

      <h2>Data quality</h2>
      <QueryState q={dq}>
        {(d) => (
          <div style={{ maxWidth: "46rem" }}>
            <div className="row" style={{ marginBottom: "0.5rem" }}>
              <RagDot status={d.status} />
              <b>{ragLabel(d.status)}</b>
              <span className="muted small">
                {d.summary.PASS} pass · {d.summary.WARN} warn · {d.summary.FAIL} fail
              </span>
            </div>
            <div className="muted small" style={{ marginBottom: "0.5rem" }}>
              {d.stubs.n_securities} names · {d.stubs.sector_unknown} unknown sector ·
              {" "}{d.stubs.country_stub_US} country stubbed “US”
            </div>
            <table className="tufte">
              <thead><tr><th className="label">Check</th><th className="label">Level</th><th className="label">Detail</th></tr></thead>
              <tbody>
                {d.checks.map((c, i) => (
                  <tr key={i}>
                    <td className="label">{c.name}</td>
                    <td className="label"><RagDot status={c.level} /> {c.level}</td>
                    <td className="label muted small">{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>

      <h2>VaR backtest — HistFull</h2>
      <QueryState q={bt}>
        {(d) =>
          d.status !== "ok" ? (
            <div className="muted small">insufficient history</div>
          ) : (
            <div style={{ maxWidth: "46rem" }}>
              <div className="row" style={{ marginBottom: "0.6rem" }}>
                <RagDot status={d.kupiec_reject ? "red" : "green"} />
                <b>Kupiec {d.kupiec_reject ? "reject" : "pass"}</b>
                <span className="muted small">
                  method {d.method} · {d.alpha! * 100}% · {d.window}d window
                </span>
              </div>
              <table className="tufte" style={{ maxWidth: "30rem" }}>
                <tbody>
                  <tr><td className="label">Tested days</td><td>{d.tested}</td></tr>
                  <tr><td className="label">Exceptions</td><td>{d.exceptions}</td></tr>
                  <tr><td className="label">Expected</td><td>{num(d.expected, 1)}</td></tr>
                  <tr><td className="label">Breach rate</td><td>{pct(d.rate ?? null)}</td></tr>
                  <tr><td className="label">Kupiec LR</td><td>{num(d.kupiec_LR, 3)} (crit {d.kupiec_crit})</td></tr>
                  <tr><td className="label">Kupiec reject?</td><td>{d.kupiec_reject ? "yes" : "no"}</td></tr>
                </tbody>
              </table>
              {!!d.n_exception_dates && (
                <div className="muted small" style={{ marginTop: "0.5rem" }}>
                  {d.n_exception_dates} exception dates{d.exception_dates?.length ? `: ${d.exception_dates.slice(0, 12).join(", ")}${d.n_exception_dates > 12 ? " …" : ""}` : ""}
                </div>
              )}
            </div>
          )
        }
      </QueryState>
    </main>
  );
}
