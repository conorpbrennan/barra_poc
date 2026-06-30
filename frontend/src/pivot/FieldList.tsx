// The Excel field list (docs/vite-ui-plan.md §5): source dimensions + measures from /dims, dragged/
// clicked into four zones that map straight to the /pivot request — ROWS→rows, COLUMNS→cols,
// VALUES→measures, FILTERS→filters. dnd-kit gives drag-reorder of the ROWS zone (order IS the drill
// hierarchy). Only allowlisted fields can be added; the server still validates (defense in depth).
import { useState } from "react";
import {
  DndContext, closestCenter, PointerSensor, useSensor, useSensors, DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext, arrayMove, horizontalListSortingStrategy, useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { Dims } from "../api/types";
import type { PivotConfig } from "./usePivot";

function Chip({ id, label, onRemove, sortable }: { id: string; label: string; onRemove: () => void; sortable?: boolean }) {
  const s = useSortable({ id, disabled: !sortable });
  const style = sortable
    ? { transform: CSS.Transform.toString(s.transform), transition: s.transition }
    : undefined;
  return (
    <span ref={sortable ? s.setNodeRef : undefined} style={style}
      className="tag" {...(sortable ? { ...s.attributes, ...s.listeners } : {})}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      >
      {sortable && <span style={{ cursor: "grab", color: "var(--faint)" }}>⠿ </span>}
      {label}
      <button onClick={onRemove} title="remove"
        style={{ border: "none", padding: "0 0 0 0.3rem", color: "var(--faint)" }}>×</button>
    </span>
  );
}

function Zone({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "0.6rem" }}>
      <div className="muted" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: "0.2rem" }}>{title}</div>
      <div className="wrap" style={{ minHeight: "1.5rem", gap: "0.3rem" }}>{children}</div>
    </div>
  );
}

export function FieldList({
  cfg, setCfg, dims, onApply,
}: { cfg: PivotConfig; setCfg: (u: (c: PivotConfig) => PivotConfig) => void; dims: Dims; onApply: () => void }) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));
  const [filterDim, setFilterDim] = useState<string>("");

  const scenCtx = cfg.cols.includes("ScenarioSet") || "ScenarioSet" in cfg.filters;
  const scenMeasureNoCtx = cfg.measures.some((m) => dims.scenario_dependent.includes(m)) && !scenCtx;

  const addRow = (d: string) => setCfg((c) => (c.rows.includes(d) ? c : { ...c, rows: [...c.rows, d] }));
  const addCol = (d: string) => setCfg((c) => ({ ...c, cols: [d] }));
  const addMeasure = (m: string) => setCfg((c) => (c.measures.includes(m) ? c : { ...c, measures: [...c.measures, m] }));
  const remRow = (d: string) => setCfg((c) => ({ ...c, rows: c.rows.filter((x) => x !== d) }));
  const remCol = (d: string) => setCfg((c) => ({ ...c, cols: c.cols.filter((x) => x !== d) }));
  const remMeasure = (m: string) => setCfg((c) => ({ ...c, measures: c.measures.filter((x) => x !== m) }));
  const setFilter = (d: string, members: string[]) =>
    setCfg((c) => {
      const f = { ...c.filters };
      if (members.length) f[d] = members; else delete f[d];
      return { ...c, filters: f };
    });

  const onDragEnd = (e: DragEndEvent) => {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    setCfg((c) => {
      const oldI = c.rows.indexOf(String(active.id));
      const newI = c.rows.indexOf(String(over.id));
      if (oldI < 0 || newI < 0) return c;
      return { ...c, rows: arrayMove(c.rows, oldI, newI) };
    });
  };

  return (
    <div style={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 2,
      padding: "0.7rem", width: "17rem", flexShrink: 0 }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: "0.5rem" }}>
        <b style={{ fontSize: 13 }}>Fields</b>
        <button className="primary" onClick={onApply}>Apply</button>
      </div>

      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
        <Zone title="Rows (drag to reorder = drill order)">
          <SortableContext items={cfg.rows} strategy={horizontalListSortingStrategy}>
            {cfg.rows.map((d) => <Chip key={d} id={d} label={d} onRemove={() => remRow(d)} sortable />)}
          </SortableContext>
        </Zone>
      </DndContext>

      <Zone title="Columns (≤1)">
        {cfg.cols.map((d) => <Chip key={d} id={d} label={d} onRemove={() => remCol(d)} />)}
      </Zone>
      <Zone title="Values">
        {cfg.measures.map((m) => <Chip key={m} id={m} label={m} onRemove={() => remMeasure(m)} />)}
      </Zone>
      <Zone title="Filters">
        {Object.entries(cfg.filters).map(([d, v]) => (
          <Chip key={d} id={d} label={`${d}=${v.length > 1 ? `${v.length}` : v[0]}`} onRemove={() => setFilter(d, [])} />
        ))}
      </Zone>

      {scenMeasureNoCtx && (
        <div className="small rag-amber" style={{ marginBottom: "0.5rem" }}>
          ⚠ scenario measure needs a ScenarioSet — add it to Filters or Columns, else cells are blank.
        </div>
      )}

      <hr className="rule" style={{ margin: "0.6rem 0" }} />

      <div style={{ maxHeight: "12rem", overflowY: "auto" }}>
        <div className="muted small" style={{ marginBottom: "0.2rem" }}>Dimensions</div>
        {dims.dimensions.map((d) => (
          <div key={d} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
            <span>{d}</span>
            <span className="row" style={{ gap: "0.15rem" }}>
              <button onClick={() => addRow(d)} title="to rows" style={{ padding: "0 0.3rem" }}>R</button>
              <button onClick={() => addCol(d)} title="to columns" style={{ padding: "0 0.3rem" }}>C</button>
              <button onClick={() => setFilterDim(d)} title="filter" style={{ padding: "0 0.3rem" }}>F</button>
            </span>
          </div>
        ))}
      </div>

      {filterDim && (
        <FilterPicker dim={filterDim} members={dims.members[filterDim] ?? []}
          selected={cfg.filters[filterDim] ?? []}
          onClose={() => setFilterDim("")}
          onChange={(ms) => setFilter(filterDim, ms)} />
      )}

      <div className="muted small" style={{ margin: "0.5rem 0 0.2rem" }}>Measures</div>
      <div style={{ maxHeight: "12rem", overflowY: "auto" }}>
        {dims.measures.map((m) => (
          <div key={m} className="row" style={{ justifyContent: "space-between", fontSize: 12.5 }}>
            <span className={dims.scenario_dependent.includes(m) ? "" : ""}>{m}</span>
            <button onClick={() => addMeasure(m)} title="to values" style={{ padding: "0 0.3rem" }}>+</button>
          </div>
        ))}
      </div>
    </div>
  );
}

function FilterPicker({
  dim, members, selected, onChange, onClose,
}: { dim: string; members: string[]; selected: string[]; onChange: (m: string[]) => void; onClose: () => void }) {
  const [sel, setSel] = useState<string[]>(selected);
  const toggle = (m: string) => setSel((s) => (s.includes(m) ? s.filter((x) => x !== m) : [...s, m]));
  return (
    <div style={{ border: "1px solid var(--line)", borderRadius: 2, padding: "0.5rem", margin: "0.4rem 0",
      background: "var(--bg)" }}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: "0.3rem" }}>
        <b className="small">Filter {dim}</b>
        <button onClick={() => { onChange(sel); onClose(); }}>done</button>
      </div>
      <div style={{ maxHeight: "10rem", overflowY: "auto" }}>
        {members.map((m) => (
          <label key={m} className="row small" style={{ gap: "0.3rem" }}>
            <input type="checkbox" checked={sel.includes(m)} onChange={() => toggle(m)} /> {m}
          </label>
        ))}
      </div>
    </div>
  );
}
