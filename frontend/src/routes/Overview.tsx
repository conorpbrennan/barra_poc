// The Overview monitor (Few's "monitor" sense): everything that answers "are we OK now" on one
// screen, no scroll for the summary. Hero risk numbers each with an inline sparkline, the
// limits/DQ/backtest RAG strip, top factor exposures as direct-labelled bars, and the QoQ
// what-changed summary. Each group links into its lens.
import { Link } from "react-router-dom";
import { useApp } from "../context/AppContext";
import {
  useTrends, useDrawdown, useWhatif, useLimits, useDq, useBacktest, useExposures, useWhatChanged,
} from "../api/hooks";
import { Sparkline, BulletGraph, LabelBar } from "../components/svg";
import { RagDot } from "../components/ui";
import { pct, signedPct, num, ragLabel } from "../lib/format";
import type { Rec } from "../api/types";

function lastNum(recs: Rec[] | undefined, key: string): number | null {
  if (!recs?.length) return null;
  const v = recs[recs.length - 1][key];
  return typeof v === "number" ? v : null;
}
function series(recs: Rec[] | undefined, key: string): (number | null)[] {
  return (recs ?? []).map((r) => (typeof r[key] === "number" ? (r[key] as number) : null));
}

export function Overview() {
  const { date, scenario, book, ready } = useApp();

  const trends = useTrends(scenario, "Total VaR 99,Scenario ES 97.5,Risk HHI");
  const dd = useDrawdown(scenario, date, book);
  const wi = useWhatif(date, book, []);
  const limits = useLimits(date, scenario, book);
  const dq = useDq();
  const bt = useBacktest("HistFull", date, book);
  const exposures = useExposures(date);
  const changed = useWhatChanged(date, undefined, book);

  if (!ready) return <main className="lens"><div className="spin">loading…</div></main>;

  const tr = trends.data?.records;
  const totVar = lastNum(tr, "Total VaR 99");
  const es = lastNum(tr, "Scenario ES 97.5");
  const hhi = lastNum(tr, "Risk HHI");
  const ddVal = dd.data?.status === "ok" ? dd.data.max_drawdown ?? null : null;
  const before = wi.data?.before;

  // top factor exposures (by |Net exposure|), Market first as it dominates
  const exps = (exposures.data ?? [])
    .map((r) => ({ factor: String(r.Factor ?? ""), v: Number(r["Net exposure"] ?? 0) }))
    .filter((e) => e.factor)
    .sort((a, b) => Math.abs(b.v) - Math.abs(a.v))
    .slice(0, 7);
  const maxExp = Math.max(...exps.map((e) => Math.abs(e.v)), 1);

  return (
    <main className="lens">
      <h1>Overview</h1>
      <p className="sub">{book} · as-of {date} · scenario {scenario}</p>

      {/* ---- hero risk numbers, each with an inline sparkline ---- */}
      <div className="hgroup">
        <div>
          <h2 style={{ marginTop: 0 }}>Risk</h2>
          <HeroNum k="Total VaR 99" v={pct(totVar)} spark={series(tr, "Total VaR 99")} to="/trends" />
          <HeroNum k="ES 97.5" v={pct(es)} spark={series(tr, "Scenario ES 97.5")} to="/trends" />
          <HeroNum k="Max drawdown" v={pct(ddVal)} spark={(dd.data?.path ?? []).map((p) => p.drawdown)}
            to="/trends" />
          <HeroNum k="Risk HHI" v={num(hhi, 3)} spark={series(tr, "Risk HHI")} to="/trends" />
        </div>

        {/* ---- limits as bullet graphs ---- */}
        <div>
          <h2 style={{ marginTop: 0 }}><Link to="/checks">Limits</Link></h2>
          {limits.data?.configured ? (
            limits.data.checks.map((c) => (
              <div key={c.name} style={{ marginBottom: "0.45rem" }}>
                <div className="row small" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{c.name}</span>
                  <span className="num">
                    <RagDot status={c.status === "breach" ? "breach" : c.status} /> {pct(c.value)}
                  </span>
                </div>
                <BulletGraph value={c.value} warn={c.warn} limit={c.limit} status={c.status} />
              </div>
            ))
          ) : (
            <div className="muted small">no limits configured</div>
          )}
        </div>

        {/* ---- trade / quality ---- */}
        <div>
          <h2 style={{ marginTop: 0 }}>Trade / quality</h2>
          <KV k="Gross" v={num(before?.gross, 2)} />
          <KV k="Net" v={num(before?.net, 2)} />
          <KV k="Specific vol" v={pct(before?.specific_vol)} />
          <div className="row small" style={{ marginTop: "0.5rem", gap: "1.1rem" }}>
            <span><RagDot status={limits.data?.status} /> <Link to="/checks">limits {ragLabel(limits.data?.status)}</Link></span>
          </div>
          <div className="row small" style={{ gap: "1.1rem" }}>
            <span><RagDot status={dq.data?.status} /> <Link to="/checks">DQ {ragLabel(dq.data?.status)}</Link></span>
            <span><RagDot status={bt.data?.basel_zone} /> <Link to="/checks">backtest {bt.data?.basel_zone ?? "—"}</Link></span>
          </div>
        </div>
      </div>

      <hr className="rule" />

      <div className="hgroup">
        {/* ---- top factor exposures ---- */}
        <div style={{ gridColumn: "span 2" }}>
          <h2 style={{ marginTop: 0 }}><Link to="/attribution">Top factor exposures</Link></h2>
          {exps.length ? exps.map((e) => (
            <LabelBar key={e.factor} label={e.factor} value={e.v} max={maxExp} neg />
          )) : <div className="muted small">—</div>}
        </div>

        {/* ---- what changed (QoQ) ---- */}
        <div>
          <h2 style={{ marginTop: 0 }}><Link to="/changes">What changed (QoQ)</Link></h2>
          {changed.data ? (
            <div className="small">
              <div className="num" style={{ marginBottom: "0.3rem" }}>
                {changed.data.from} → {changed.data.to}
              </div>
              <div className="muted">
                +{changed.data.positions.n_entered} entered · −{changed.data.positions.n_exited} exited
              </div>
              <div style={{ marginTop: "0.4rem" }}>
                {changed.data.exposure_attribution.slice(0, 3).map((r) => {
                  const d = Number(r.delta ?? 0);
                  return (
                    <div key={String(r.factor)} className="num">
                      {String(r.factor)} {d >= 0 ? "↑" : "↓"} {signedPct(d, 1)}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : <div className="muted small">{changed.isLoading ? "loading…" : "—"}</div>}
        </div>
      </div>
    </main>
  );
}

function HeroNum({ k, v, spark, to }: { k: string; v: string; spark: (number | null)[]; to: string }) {
  return (
    <Link to={to} style={{ color: "inherit", display: "block", marginBottom: "0.5rem" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <div className="hero"><span className="v">{v}</span></div>
          <div className="k">{k}</div>
        </div>
        <Sparkline values={spark} />
      </div>
    </Link>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="row" style={{ justifyContent: "space-between", maxWidth: "11rem" }}>
      <span className="muted small">{k}</span>
      <span className="num">{v}</span>
    </div>
  );
}
