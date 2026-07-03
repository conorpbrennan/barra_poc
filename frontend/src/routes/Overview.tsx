// The Overview monitor (Few's "monitor" sense): everything that answers "are we OK now" on one
// screen, no scroll for the summary. Hero risk numbers each with an inline sparkline, the
// limits/DQ/backtest RAG strip, top factor exposures as direct-labelled bars, and the QoQ
// what-changed summary. Each group links into its lens.
import { Link } from "react-router-dom";
import { useApp } from "../context/AppContext";
import {
  useTrends, useWhatif, useLimits, useDq, useBacktest, useExposures, useWhatChanged,
  useContributions, usePnlLinkage,
} from "../api/hooks";
import { Sparkline, BulletGraph, LabelBar } from "../components/svg";
import { StreamPanel } from "../components/StreamPanel";
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

  const trends = useTrends(scenario, "Model vol,Scenario VaR 99,Scenario ES 97.5");
  const wi = useWhatif(date, book, []);
  const limits = useLimits(date, scenario, book);
  const dq = useDq();
  const bt = useBacktest("HistFull", date, book);
  const exposures = useExposures(date);
  const contrib = useContributions(date, book);
  const lk = usePnlLinkage(3, undefined, book);
  const changed = useWhatChanged(date, undefined, book);

  if (!ready) return <main className="lens"><div className="spin">loading…</div></main>;

  const tr = trends.data?.records;
  const dates = (tr ?? []).map((r) => String(r.Date ?? "").slice(0, 10));
  const scenVar = lastNum(tr, "Scenario VaR 99");
  const es = lastNum(tr, "Scenario ES 97.5");
  const before = wi.data?.before;
  const factorShare = contrib.data?.factor_share ?? null;

  // ch-09 step 1: contribution to risk (CTV), not raw exposure — the primary bars
  const ctv = (contrib.data?.factors ?? []).slice(0, 7);
  const maxCtv = Math.max(...ctv.map((r) => Math.abs((r.pct_of_variance ?? 0) * 100)), 1e-6);

  // risk↔PnL reconcile status (Chris's step 4): genuine breaches only — an
  // exposure_migration driver is a band artifact, not a risk the decomposition missed
  const lkRows = lk.data ? [...lk.data.rows, lk.data.book_total] : [];
  const genuine = lkRows.filter(
    (r) => r.verdict === "investigate" && r.driver?.kind !== "exposure_migration");
  const stressed = lkRows.filter((r) => r.verdict === "stress");
  const lkStatus = !lk.data ? undefined
    : genuine.length ? "red" : (stressed.length ? "amber" : "green");
  const lkLabel = !lk.data ? "—"
    : genuine.length ? `${genuine.length} to investigate (${genuine.map((r) => r.name).join(", ")})`
    : stressed.length ? "stress regime" : "within band";

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
          <HeroNum k="Model vol (1d) — the reference" v={pct(before?.model_vol_1d ?? null, 2)}
            spark={series(tr, "Model vol")} sparkLabels={dates} to="/attribution" />
          <HeroNum k="Factor / specific variance"
            v={factorShare === null ? "—" : `${pct(factorShare, 0)} / ${pct(1 - factorShare, 0)}`}
            spark={[]} to="/attribution" />
          <HeroNum k="Scenario VaR 99 (limit metric)" v={pct(scenVar)}
            spark={series(tr, "Scenario VaR 99")} sparkLabels={dates} to="/trends" />
          <HeroNum k="ES 97.5 (limit metric)" v={pct(es)} spark={series(tr, "Scenario ES 97.5")}
            sparkLabels={dates} to="/trends" />
          <HeroNum k="Top-5 risk share" v={pct(before?.top5_ctr_share ?? null, 1)} spark={[]}
            to="/attribution" />
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
            <span><RagDot status={bt.data ? (bt.data.kupiec_reject ? "red" : "green") : undefined} />{" "}
              <Link to="/checks">backtest {bt.data?.kupiec_reject === undefined ? "—"
                : bt.data.kupiec_reject ? "Kupiec reject" : "Kupiec pass"}</Link></span>
          </div>
          <div className="row small">
            <span><RagDot status={lkStatus} />{" "}
              <Link to="/attribution">reconcile {lkLabel}</Link></span>
          </div>
        </div>
      </div>

      <hr className="rule" />

      <div className="hgroup">
        {/* ---- step 1: contribution to risk (CTV), exposures as the secondary read ---- */}
        <div style={{ gridColumn: "span 2" }}>
          <h2 style={{ marginTop: 0 }}><Link to="/attribution">Top risk contributions (CTV)</Link></h2>
          {ctv.length ? ctv.map((r) => (
            <LabelBar key={r.factor} label={r.factor}
              value={(r.pct_of_variance ?? 0) * 100} max={maxCtv} suffix="%" neg />
          )) : <div className="muted small">—</div>}
          <p className="muted small" style={{ margin: "0.2rem 0 0.6rem" }}>
            share of total variance; negative = hedges the book
          </p>
          <div className="muted small" style={{ marginBottom: "0.2rem" }}>Net exposures</div>
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

      <hr className="rule" />

      {/* ---- morning risk summary, in the desk risk-manager voice (CHRIS_VOICE) ---- */}
      <div style={{ maxWidth: "46rem" }}>
        <h2>Risk-manager summary</h2>
        <StreamPanel path="/overview/analysis"
          body={{ date, book, set: scenario }}
          cacheKey={`overview:${date}:${book}:${scenario}`}
          label="Generate morning summary" />
      </div>
    </main>
  );
}

function HeroNum({ k, v, spark, sparkLabels, to }: {
  k: string; v: string; spark: (number | null)[]; sparkLabels?: string[]; to: string;
}) {
  return (
    <Link to={to} style={{ color: "inherit", display: "block", marginBottom: "0.5rem" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <div className="hero"><span className="v">{v}</span></div>
          <div className="k">{k}</div>
        </div>
        <Sparkline values={spark} labels={sparkLabels} fmt={(x) => pct(x, 2)} />
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
