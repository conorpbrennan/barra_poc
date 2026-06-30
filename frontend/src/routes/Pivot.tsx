// Pivot workspace (docs/vite-ui-plan.md §5): the Excel-style field list, the server-driven drill
// grid (or chart mode), the saved-view Repository, and on-demand /analysis commentary. The grid is a
// pure renderer — every number comes from a /pivot call behind the cube's allowlist guard.
import { Suspense, lazy, useEffect, useState } from "react";
import { useApp } from "../context/AppContext";
import { useDims } from "../api/hooks";
import { usePivot } from "../pivot/usePivot";
import { FieldList } from "../pivot/FieldList";
import { PivotGrid } from "../pivot/PivotGrid";
// Vega is ~heavy; only load it when the user switches to chart mode.
const ChartMode = lazy(() => import("../pivot/ChartMode").then((m) => ({ default: m.ChartMode })));
import { Repository } from "../pivot/Repository";
import { StreamPanel } from "../components/StreamPanel";
import { QueryState } from "../components/ui";
import type { ViewState } from "../api/types";

export function Pivot() {
  const { date, scenario } = useApp();
  const dimsQ = useDims();
  const [mode, setMode] = useState<"grid" | "chart">("grid");
  const [showRepo, setShowRepo] = useState(false);
  const [showAnalysis, setShowAnalysis] = useState(false);

  // seed the pivot filters from the global context bar (§9): Date + ScenarioSet, overridable.
  const pivot = usePivot({
    rows: ["Sector"], measures: ["Net exposure", "Scenario VaR 99"],
    filters: { Date: [date], ScenarioSet: [scenario] },
    totals: true, heat: true, asPct: false, prec: 3,
  });
  const { cfg, setCfg, reload, toggleExpand, flat, colMembers, grand, warning, loading, error } = pivot;

  // keep Date/ScenarioSet in sync when the context bar changes (unless the user overrode them away)
  useEffect(() => {
    setCfg((c) => ({ ...c, filters: { ...c.filters, Date: [date], ScenarioSet: [scenario] } }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [date, scenario]);

  // first load
  useEffect(() => {
    if (dimsQ.data) reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dimsQ.data]);

  const loadViewState = (s: ViewState, name: string) => {
    setCfg((c) => ({
      ...c,
      rows: s.rows ?? c.rows, cols: s.cols ?? [], measures: s.measures ?? c.measures,
      filters: s.filters ?? c.filters, totals: s.row_tot ?? c.totals,
      heat: s.heat ?? c.heat, asPct: s.as_pct ?? c.asPct, prec: s.prec ?? c.prec,
    }));
    setMode(s.render === "chart" ? "chart" : "grid");
    setTimeout(() => reload(), 0);
    document.title = `${name} · pivot`;
  };

  const currentState: ViewState = {
    rows: cfg.rows, cols: cfg.cols, measures: cfg.measures, filters: cfg.filters,
    row_tot: cfg.totals, as_pct: cfg.asPct, heat: cfg.heat, prec: cfg.prec,
    render: mode,
  };

  const analysisBody = {
    rows: cfg.rows.join(","), cols: cfg.cols.join(","), measures: cfg.measures.join(","),
    filters: JSON.stringify(cfg.filters), totals: cfg.totals, name: "pivot view",
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
          <label className="row small"><input type="checkbox" checked={cfg.heat} onChange={(e) => setCfg((c) => ({ ...c, heat: e.target.checked }))} /> heat</label>
          <label className="row small"><input type="checkbox" checked={cfg.asPct} onChange={(e) => setCfg((c) => ({ ...c, asPct: e.target.checked }))} /> %</label>
          <label className="row small"><input type="checkbox" checked={cfg.totals} onChange={(e) => setCfg((c) => ({ ...c, totals: e.target.checked }))} /> totals</label>
        </div>
      </div>

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
                  <ChartMode cfg={cfg} />
                </Suspense>
              )}
            </div>
            {showRepo && <Repository currentState={currentState} onLoad={loadViewState} />}
          </div>
        )}
      </QueryState>

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
