// Pivot workspace (docs/vite-ui-plan.md §5): the Excel-style field list, the server-driven drill
// grid (or chart mode), the saved-view Repository, and on-demand /analysis commentary. The grid is a
// pure renderer — every number comes from a /pivot call behind the cube's allowlist guard.
import { Suspense, lazy, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useApp } from "../context/AppContext";
import { useDims, useMeta, useWhatif } from "../api/hooks";
import { usePivot, hypoParams, type PivotConfig } from "../pivot/usePivot";
import { FieldList } from "../pivot/FieldList";
import { PivotGrid } from "../pivot/PivotGrid";
// Vega is ~heavy; only load it when the user switches to chart mode.
const ChartMode = lazy(() => import("../pivot/ChartMode").then((m) => ({ default: m.ChartMode })));
import { Repository } from "../pivot/Repository";
import { StreamPanel } from "../components/StreamPanel";
import { QueryState } from "../components/ui";
import type { ViewState } from "../api/types";

// ---- Hypothetical bar: price THIS pivot under what-if trades and/or factor shocks. Each query
// runs on a transient cube branch/scenario (stateless server-side); the amber strip makes it
// impossible to mistake a hypothetical grid for the held book. Not persisted in saved views. ----
function HypoBar({ cfg, date, book, apply }: {
  cfg: PivotConfig; date: string; book: string;
  apply: (next: PivotConfig) => void;
}) {
  const { data: meta } = useMeta();
  const boot = useWhatif(date, book, []);          // holdings + universe for the trade picker
  const [pos, setPos] = useState("");
  const [wgt, setWgt] = useState("");
  const [fac, setFac] = useState("");
  const [sig, setSig] = useState("");
  const holdings = boot.data?.holdings ?? [];
  const universe = boot.data?.universe ?? [];
  const held = new Map(holdings.map((h) => [h.position, h]));
  const names = [...holdings,
    ...universe.filter((u) => !held.has(u.position)).map((u) => ({ ...u, weight: 0 }))];
  const factors = (meta?.factors ?? []).filter((f) => f !== "Market");
  const active = cfg.whatif.length > 0 || Object.keys(cfg.shocks).length > 0;

  const addTrade = () => {
    const w = Number(wgt);
    if (!pos || Number.isNaN(w)) return;
    const t = names.find((n) => n.position === pos);
    apply({ ...cfg, whatif: [...cfg.whatif.filter((x) => x.position !== pos),
                             { position: pos, ticker: t?.ticker ?? pos, weight: w }] });
    setPos(""); setWgt("");
  };
  const addShock = () => {
    const s = Number(sig);
    if (!fac || Number.isNaN(s) || s === 0) return;
    apply({ ...cfg, shocks: { ...cfg.shocks, [fac]: s } });
    setFac(""); setSig("");
  };

  return (
    <div className="small" style={{ margin: "0.4rem 0", padding: "0.45rem 0.6rem",
      borderLeft: "3px solid #b07d2b", background: "rgba(176,125,43,0.06)" }}>
      <div className="row" style={{ flexWrap: "wrap", gap: "0.4rem 1rem" }}>
        <strong style={{ color: "#b07d2b" }}>Hypothetical</strong>
        <span className="row" style={{ gap: "0.3rem" }}>
          <select value={pos} onChange={(e) => setPos(e.target.value)}>
            <option value="">trade name…</option>
            {names.map((n) => <option key={n.position} value={n.position}>{n.ticker}</option>)}
          </select>
          <input type="number" step={0.005} placeholder="weight" style={{ width: "4.6rem" }}
            value={wgt} onChange={(e) => setWgt(e.target.value)} />
          <button disabled={!pos || wgt === ""} onClick={addTrade}>add trade</button>
        </span>
        <span className="row" style={{ gap: "0.3rem" }}>
          <select value={fac} onChange={(e) => setFac(e.target.value)}>
            <option value="">shock factor…</option>
            {factors.map((f) => <option key={f}>{f}</option>)}
          </select>
          <input type="number" step={0.5} placeholder="σ" style={{ width: "3.4rem" }}
            value={sig} onChange={(e) => setSig(e.target.value)} />
          <button disabled={!fac || sig === ""} onClick={addShock}>add shock</button>
        </span>
        {active && (
          <button onClick={() => apply({ ...cfg, whatif: [], shocks: {} })}>clear all</button>
        )}
      </div>
      {active && (
        <div className="row" style={{ flexWrap: "wrap", gap: "0.3rem", marginTop: "0.35rem" }}>
          {cfg.whatif.map((t) => (
            <button key={t.position} title="remove"
              onClick={() => apply({ ...cfg, whatif: cfg.whatif.filter((x) => x.position !== t.position) })}>
              {t.ticker.toUpperCase()} → {(t.weight * 100).toFixed(1)}% ×
            </button>
          ))}
          {Object.entries(cfg.shocks).map(([f, s]) => (
            <button key={f} title="remove"
              onClick={() => { const sh = { ...cfg.shocks }; delete sh[f]; apply({ ...cfg, shocks: sh }); }}>
              {f} {s > 0 ? "+" : ""}{s}σ ×
            </button>
          ))}
          <span className="muted">
            — every number in this grid is branch-priced under the hypothetical
            {cfg.whatif.length ? "" : ""}. Attribution measures stay the held book.
          </span>
        </div>
      )}
    </div>
  );
}

export function Pivot() {
  const { date, scenario, book } = useApp();
  const dimsQ = useDims();
  const [mode, setMode] = useState<"grid" | "chart">("grid");
  const [showRepo, setShowRepo] = useState(false);
  const [showAnalysis, setShowAnalysis] = useState(false);
  const [showHypo, setShowHypo] = useState(false);
  // the currently-loaded saved view (name + its description), shown in the bottom description pane
  const [loadedView, setLoadedView] = useState<{ name: string; description?: string } | null>(null);
  // a charted view is self-describing: its named queries + Vega-Lite spec(s), rendered verbatim
  const [chartView, setChartView] = useState<Pick<ViewState, "queries" | "chart"> | null>(null);

  // seed the pivot filters from the global context bar (§9): Date + ScenarioSet, overridable.
  const pivot = usePivot({
    rows: ["Sector"], measures: ["Net exposure", "Scenario VaR 99"],
    filters: { Date: [date], ScenarioSet: [scenario] },
    totals: true, heat: true, asPct: false, prec: 3,
  });
  const { cfg, setCfg, reload, toggleExpand, flat, colMembers, grand, warning, loading, error } = pivot;

  // Cross-lens drill link (?drill=<json {rows, cols?, measures, filters}>, e.g. from the
  // Attribution reconcile drawer): captured ONCE at mount, consumed inside the fold effect below
  // so mount issues exactly one reload — a separate effect would race the fold's own reload and
  // the loser's response would clobber the grid (blank measure columns). Deliberately NOT an
  // effect dependency: consuming it must not re-trigger the fold.
  const [searchParams, setSearchParams] = useSearchParams();
  const [pendingDrill, setPendingDrill] = useState<
    Partial<Pick<PivotConfig, "rows" | "cols" | "measures" | "filters">> | null>(() => {
    const raw = searchParams.get("drill");
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return null; }
  });

  // Reload whenever the cube is ready or the global context (date / scenario) changes, folding them
  // into the pivot filters AND re-querying — so changing the scenario dropdown updates the numbers
  // immediately (previously it updated the filter chip but not the grid). ScenarioSet is only sliced
  // when it is NOT already on an axis: a view may put ScenarioSet on Rows/Columns to COMPARE across
  // sets (e.g. Concentration — Risk HHI), and those must not be collapsed to the single global set.
  useEffect(() => {
    if (!dimsQ.data || !date) return;
    if (pendingDrill) {
      const next: PivotConfig = { ...cfg,
        rows: pendingDrill.rows ?? cfg.rows, cols: pendingDrill.cols ?? [],
        measures: pendingDrill.measures ?? cfg.measures,
        filters: pendingDrill.filters ?? cfg.filters,
        whatif: [], shocks: {} };
      setCfg(next);
      setMode("grid");
      reload(next);
      setLoadedView({ name: "drill-through", description: "opened from another lens" });
      setPendingDrill(null);
      setSearchParams({}, { replace: true });
      return;
    }
    const onAxis = cfg.rows.includes("ScenarioSet") || cfg.cols.includes("ScenarioSet");
    const filters: Record<string, string[]> = { ...cfg.filters, Date: [date] };
    if (onAxis) delete filters.ScenarioSet;
    else filters.ScenarioSet = [scenario];
    const next = { ...cfg, filters };
    setCfg(next);
    reload(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dimsQ.data, date, scenario]);

  const loadViewState = (s: ViewState, name: string) => {
    // Build the next config explicitly and hand it straight to reload(). Do NOT rely on setCfg +
    // a deferred reload() — reload closes over the CURRENT-render cfg, so a bare reload() fired
    // before React re-renders would query with the *previous* view's config (the "first click shows
    // the wrong report, second click is right" bug). Passing `next` bypasses that stale closure.
    const next: PivotConfig = {
      ...cfg,
      rows: s.rows ?? cfg.rows, cols: s.cols ?? [], measures: s.measures ?? cfg.measures,
      filters: s.filters ?? cfg.filters, totals: s.row_tot ?? cfg.totals,
      heat: s.heat ?? cfg.heat, asPct: s.as_pct ?? cfg.asPct, prec: s.prec ?? cfg.prec,
      whatif: [], shocks: {},   // a saved view is a canonical report — never load it hypothetical
    };
    setCfg(next);
    setMode(s.render === "chart" ? "chart" : "grid");
    reload(next);
    setLoadedView({ name, description: s.description });
    // capture the self-describing chart (queries + spec) so chart mode renders it verbatim
    setChartView(s.render === "chart" && s.chart ? { queries: s.queries, chart: s.chart } : null);
    document.title = `${name} · pivot`;
  };

  const currentState: ViewState = {
    rows: cfg.rows, cols: cfg.cols, measures: cfg.measures, filters: cfg.filters,
    row_tot: cfg.totals, as_pct: cfg.asPct, heat: cfg.heat, prec: cfg.prec,
    render: mode, description: loadedView?.description,
  };

  const hypoActive = cfg.whatif.length > 0 || Object.keys(cfg.shocks).length > 0;
  const analysisBody = {
    rows: cfg.rows.join(","), cols: cfg.cols.join(","), measures: cfg.measures.join(","),
    filters: JSON.stringify(cfg.filters), totals: cfg.totals,
    name: hypoActive ? "pivot view (HYPOTHETICAL)" : "pivot view",
    ...hypoParams(cfg),
  };

  return (
    <main className="lens" style={{ paddingRight: "1rem" }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>Pivot</h1>
          <p className="sub">Server-side pivot over the cube · the grid never groups or sums</p>
        </div>
        <div className="row">
          <button className={mode === "grid" ? "primary" : ""} onClick={() => setMode("grid")}>Grid</button>
          <button className={mode === "chart" ? "primary" : ""} onClick={() => setMode("chart")}>Chart</button>
          <button onClick={() => setShowRepo((s) => !s)}>{showRepo ? "Hide" : "Views"}</button>
          <button className={hypoActive ? "primary" : ""} onClick={() => setShowHypo((s) => !s)}>
            Hypothetical{hypoActive ? " ●" : ""}</button>
          <label className="row small"><input type="checkbox" checked={cfg.heat} onChange={(e) => setCfg((c) => ({ ...c, heat: e.target.checked }))} /> heat</label>
          <label className="row small"><input type="checkbox" checked={cfg.asPct} onChange={(e) => setCfg((c) => ({ ...c, asPct: e.target.checked }))} /> %</label>
          <label className="row small"><input type="checkbox" checked={cfg.totals} onChange={(e) => setCfg((c) => ({ ...c, totals: e.target.checked }))} /> totals</label>
        </div>
      </div>

      {(showHypo || hypoActive) && (
        <HypoBar cfg={cfg} date={date} book={book}
          apply={(next) => { setCfg(next); reload(next); }} />
      )}
      {warning && <div className="rag-amber small" style={{ margin: "0.3rem 0" }}>⚠ {warning}</div>}
      {error && <div className="err small">{error}</div>}

      <QueryState q={dimsQ}>
        {(dims) => (
          <div style={{ display: "flex", gap: "1rem", alignItems: "flex-start", marginTop: "0.6rem" }}>
            <FieldList cfg={cfg} setCfg={setCfg} dims={dims} onApply={() => reload()} />
            <div style={{ flex: 1, minWidth: 0 }}>
              {loading && <div className="spin">querying cube…</div>}
              {mode === "grid" ? (
                <PivotGrid flat={flat} colMembers={colMembers} measures={cfg.measures}
                  cfg={cfg} grand={grand} onToggle={toggleExpand} />
              ) : (
                <Suspense fallback={<div className="spin">loading chart…</div>}>
                  <ChartMode cfg={cfg} savedQueries={chartView?.queries} savedChart={chartView?.chart} />
                </Suspense>
              )}
            </div>
            {showRepo && <Repository currentState={currentState} onLoad={loadViewState} />}
          </div>
        )}
      </QueryState>

      {/* description pane — what the loaded view captures (reading column, et-book serif) */}
      {loadedView && (
        <div style={{ marginTop: "1rem", borderTop: "1px solid var(--line)", paddingTop: "0.7rem" }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <h2 style={{ margin: 0 }}>About this view · {loadedView.name}</h2>
            <button className="small" onClick={() => setLoadedView(null)} title="dismiss">×</button>
          </div>
          {loadedView.description
            ? <p className="reading" style={{ margin: "0.4rem 0 0" }}>{loadedView.description}</p>
            : <p className="muted small" style={{ margin: "0.4rem 0 0" }}>No description saved for this view.</p>}
        </div>
      )}

      <hr className="rule" />
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>Risk-analyst commentary</h2>
        <button onClick={() => setShowAnalysis((s) => !s)}>{showAnalysis ? "Hide" : "Show"}</button>
      </div>
      {showAnalysis && (
        <div style={{ marginTop: "0.6rem" }}>
          <StreamPanel path="/analysis" body={analysisBody}
            cacheKey={`an:${JSON.stringify(analysisBody)}`} label="Generate commentary for this view" />
        </div>
      )}
    </main>
  );
}
