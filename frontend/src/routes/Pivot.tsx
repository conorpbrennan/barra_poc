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
// impossible to mistake a hypothetical grid for the held portfolio. Not persisted in saved views. ----
function HypoBar({ cfg, date, manager, apply }: {
  cfg: PivotConfig; date: string; manager: string;
  apply: (next: PivotConfig) => void;
}) {
  const { data: meta } = useMeta();
  const boot = useWhatif(date, manager, []);          // holdings + universe for the trade picker
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
            {cfg.whatif.length ? "" : ""}. Attribution measures stay the held portfolio.
          </span>
        </div>
      )}
    </div>
  );
}

export function Pivot() {
  const { date, scenario, manager } = useApp();
  const dimsQ = useDims();
  const [mode, setMode] = useState<"grid" | "chart">("grid");
  const [showRepo, setShowRepo] = useState(false);
  const [showAnalysis, setShowAnalysis] = useState(false);
  const [showHypo, setShowHypo] = useState(false);
  // the currently-loaded saved view (name + its description), shown in the bottom description pane
  const [loadedView, setLoadedView] = useState<{ name: string; description?: string } | null>(null);
  // the loaded view's full saved state: fields Vite has no control for (date_fmt, chart/queries)
  // ride along on save instead of being dropped by a Vite round-trip
  const [loadedState, setLoadedState] = useState<ViewState | null>(null);
  // a charted view is self-describing: its named queries + Vega-Lite spec(s), rendered verbatim
  const [chartView, setChartView] = useState<Pick<ViewState, "queries" | "chart"> | null>(null);

  // seed the pivot filters from the global context bar (§9): Date + ScenarioSet, overridable.
  const pivot = usePivot({
    rows: ["Sector"], measures: ["Net exposure", "Scenario VaR 99"],
    filters: { Date: [date], ScenarioSet: [scenario] },
    totals: true, rowTot: false, hideEmpty: true, heat: true, asPct: false, prec: 3, sort: [],
    units: "dollar",
  });
  const { cfg, setCfg, reload, toggleExpand, flat, colMembers, grand, dollarMeasures, warning, loading, error } = pivot;

  // Cross-lens drill link (?drill=<json {rows, cols?, measures, filters}>, e.g. from the
  // Attribution reconcile drawer): captured ONCE at mount, consumed inside the fold effect below
  // so mount issues exactly one reload — a separate effect would race the fold's own reload and
  // the loser's response would clobber the grid (blank measure columns). Deliberately NOT an
  // effect dependency: consuming it must not re-trigger the fold.
  const [searchParams, setSearchParams] = useSearchParams();
  const [pendingDrill, setPendingDrill] = useState<
    (Partial<Pick<PivotConfig, "rows" | "cols" | "measures" | "filters">>
      & { description?: string }) | null>(() => {
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
      setLoadedView({ name: "drill-through",
        description: pendingDrill.description ?? "opened from another lens" });
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
      filters: s.filters ?? cfg.filters,
      // Streamlit's names: col_tot = Total ROW (the pinned grand/per_col margin), row_tot = Total
      // COLUMN (per_row margin, with a column dim). Pre-2026-08-21 Vite saves wrote the pinned
      // row as row_tot with no col_tot — read that form too.
      totals: s.col_tot ?? (s.row_tot && !s.cols?.length ? s.row_tot : cfg.totals),
      rowTot: s.col_tot !== undefined ? (s.row_tot ?? false) : false,
      hideEmpty: s.hide_empty ?? true,
      heat: s.heat ?? cfg.heat, asPct: s.as_pct ?? cfg.asPct, prec: s.prec ?? cfg.prec,
      sort: Array.isArray(s.sort) ? s.sort : [],
      // default $ in every report (2026-08-22); an explicit "weight" in a saved view still wins.
      units: s.units === "weight" ? "weight" : "dollar",
      whatif: [], shocks: {},   // a saved view is a canonical report — never load it hypothetical
    };
    setCfg(next);
    setMode(s.render === "chart" ? "chart" : "grid");
    reload(next);
    setLoadedView({ name, description: s.description });
    setLoadedState(s);
    // capture the self-describing chart (queries + spec) so chart mode renders it verbatim
    setChartView(s.render === "chart" && s.chart ? { queries: s.queries, chart: s.chart } : null);
    document.title = `${name} · pivot`;
  };

  // the saved form mirrors Streamlit's read_pivot_state() field for field, so a view written
  // here loads identically in the Streamlit app (and vice versa)
  const currentState: ViewState = {
    ...(loadedState ?? {}),
    rows: cfg.rows, cols: cfg.cols, measures: cfg.measures, filters: cfg.filters,
    slice_dims: Object.keys(cfg.filters),
    row_tot: cfg.rowTot, col_tot: cfg.totals, as_pct: cfg.asPct, hide_empty: cfg.hideEmpty,
    heat: cfg.heat, prec: cfg.prec, sort: cfg.sort, units: cfg.units,
    render: mode, description: loadedView?.description,
  };

  const hypoActive = cfg.whatif.length > 0 || Object.keys(cfg.shocks).length > 0;
  const analysisBody = {
    rows: cfg.rows.join(","), cols: cfg.cols.join(","), measures: cfg.measures.join(","),
    filters: JSON.stringify(cfg.filters), totals: cfg.totals,
    name: hypoActive ? "pivot view (HYPOTHETICAL)" : "pivot view",
    ...hypoParams(cfg),
  };

  // the view's display options — Streamlit's sidebar set — shown in the builder's Display zone
  const display = (
    <div className="small" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.25rem 0.6rem" }}>
      <label className="row"><input type="checkbox" checked={cfg.heat} onChange={(e) => setCfg((c) => ({ ...c, heat: e.target.checked }))} /> heat</label>
      <label className="row" title="% and fraction are formats of the same weight-unit numbers; $ re-queries the cube's Units context (measure × Manager MV)">
        units <select value={cfg.units === "dollar" ? "$" : cfg.asPct ? "%" : "fraction"} style={{ width: "5.2rem" }}
          onChange={(e) => {
            const v = e.target.value;
            const next: PivotConfig = { ...cfg, units: v === "$" ? "dollar" : "weight", asPct: v === "%" || (v === "$" ? cfg.asPct : false) };
            setCfg(next);
            if (next.units !== cfg.units) reload(next);
          }}>
          <option value="%">%</option><option value="fraction">fraction</option><option value="$">$</option>
        </select></label>
      <label className="row" title="Total row — the cube's grand / per-column margin, pinned at the bottom">
        <input type="checkbox" checked={cfg.totals} onChange={(e) => { const next = { ...cfg, totals: e.target.checked }; setCfg(next); reload(next); }} /> total row</label>
      <label className="row" title="Total column — the cube's per-row margin across the column dim (needs a column field)"
        style={{ opacity: cfg.cols.length ? 1 : 0.45 }}>
        <input type="checkbox" checked={cfg.rowTot} disabled={!cfg.cols.length}
          onChange={(e) => { const next = { ...cfg, rowTot: e.target.checked }; setCfg(next); reload(next); }} /> total column</label>
      <label className="row" title="Drop rows / columns that are entirely blank; totals are kept">
        <input type="checkbox" checked={cfg.hideEmpty} onChange={(e) => setCfg((c) => ({ ...c, hideEmpty: e.target.checked }))} /> hide empty</label>
      <label className="row" title="Decimals shown (of the percent when % is on)">
        decimals <input type="number" min={0} max={6} value={cfg.prec} style={{ width: "3rem" }}
          onChange={(e) => setCfg((c) => ({ ...c, prec: Math.max(0, Math.min(6, Number(e.target.value) || 0)) }))} /></label>
    </div>
  );

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
        </div>
      </div>

      {(showHypo || hypoActive) && (
        <HypoBar cfg={cfg} date={date} manager={manager}
          apply={(next) => { setCfg(next); reload(next); }} />
      )}
      {warning && <div className="rag-amber small" style={{ margin: "0.3rem 0" }}>⚠ {warning}</div>}
      {error && <div className="err small">{error}</div>}

      <QueryState q={dimsQ}>
        {(dims) => (
          <div style={{ display: "flex", gap: "1rem", alignItems: "flex-start", marginTop: "0.6rem" }}>
            <FieldList cfg={cfg} setCfg={setCfg} dims={dims} onApply={() => reload()} display={display} />
            <div style={{ flex: 1, minWidth: 0 }}>
              {loading && <div className="spin">querying cube…</div>}
              {mode === "grid" ? (
                <PivotGrid flat={flat} colMembers={colMembers} measures={cfg.measures}
                  cfg={cfg} grand={grand} dollarMeasures={dollarMeasures} onToggle={toggleExpand}
                  onSort={(sort) => setCfg((c) => ({ ...c, sort }))} />
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
