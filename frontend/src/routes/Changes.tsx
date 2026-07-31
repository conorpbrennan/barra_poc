// Changes lens (render_whatchanged): the deterministic QoQ diff (entered/exited/resized, factor
// drift attribution, book risk delta) + an on-demand streamed "what changed" read (§7).
import { useApp } from "../context/AppContext";
import { useWhatChanged } from "../api/hooks";
import { QueryState } from "../components/ui";
import { StreamPanel } from "../components/StreamPanel";
import { pct, signedPct, signedNum, num } from "../lib/format";

export function Changes() {
  const { date, book } = useApp();
  const q = useWhatChanged(date, undefined, book);

  return (
    <main className="lens">
      <h1>What changed (QoQ)</h1>
      <QueryState q={q}>
        {(d) => (
          <>
            <p className="sub">{book} · {d.from} → {d.to} · {d.positions.n_before}→{d.positions.n_after} names</p>

            <div className="hgroup">
              <div>
                <h2 style={{ marginTop: 0 }}>Entered ({d.positions.n_entered})</h2>
                {d.positions.entered.slice(0, 12).map((p, i) => (
                  <div key={i} className="row small" style={{ justifyContent: "space-between", maxWidth: "16rem" }}>
                    <span>{p.ticker || p.issuer}</span><span className="num rag-green">+{pct(p.weight)}</span>
                  </div>
                ))}
              </div>
              <div>
                <h2 style={{ marginTop: 0 }}>Exited ({d.positions.n_exited})</h2>
                {d.positions.exited.slice(0, 12).map((p, i) => (
                  <div key={i} className="row small" style={{ justifyContent: "space-between", maxWidth: "16rem" }}>
                    <span>{p.ticker || p.issuer}</span><span className="num rag-red">−{pct(p.weight)}</span>
                  </div>
                ))}
              </div>
              <div>
                <h2 style={{ marginTop: 0 }}>Resized</h2>
                {d.positions.resized.slice(0, 12).map((p, i) => (
                  <div key={i} className="row small" style={{ justifyContent: "space-between", maxWidth: "16rem" }}>
                    <span>{p.ticker || p.issuer}</span><span className="num">{signedPct(p.delta)}</span>
                  </div>
                ))}
              </div>
            </div>

            <h2>Factor-exposure drift</h2>
            <table className="tufte" style={{ maxWidth: "56rem" }}>
              <thead><tr><th className="label">Factor</th><th>Before</th><th>After</th><th>Δ</th><th>Entered</th><th>Exited</th><th>Reweighted</th><th>Loading drift</th></tr></thead>
              <tbody>
                {d.exposure_attribution.map((r, i) => (
                  <tr key={i}>
                    <td className="label">{String(r.factor)}</td>
                    <td>{num(Number(r.before), 2)}</td>
                    <td>{num(Number(r.after), 2)}</td>
                    <td><b>{signedNum(Number(r.delta), 2)}</b></td>
                    <td>{signedNum(Number(r.src_entered), 2)}</td>
                    <td>{signedNum(Number(r.src_exited), 2)}</td>
                    <td>{signedNum(Number(r.src_reweighted), 2)}</td>
                    <td>{signedNum(Number(r.src_loading_drift), 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h2>Book risk delta</h2>
            <table className="tufte" style={{ maxWidth: "40rem" }}>
              <thead><tr><th className="label">Measure</th><th>Before</th><th>After</th><th>Δ</th></tr></thead>
              <tbody>
                {Object.entries(d.risk).map(([k, v]) => (
                  <tr key={k}>
                    <td className="label">{k}</td>
                    <td>{num(v.before, 3)}</td>
                    <td>{num(v.after, 3)}</td>
                    <td className={v.delta != null && v.delta > 0 ? "rag-red" : "rag-green"}>{signedNum(v.delta, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h2>Risk-manager read</h2>
            <StreamPanel path="/whatchanged/analysis" cacheKey={`wc:${book}:${d.from}:${d.to}`}
              body={{ date, book }} label="What changed — commentary" />
          </>
        )}
      </QueryState>
    </main>
  );
}
