// The global context bar (docs/vite-ui-plan.md §9): manager / as-of date / scenario-set shared by
// every lens, plus the docs (📖) menu. Few: shared context once, not repeated per-panel chrome.
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { Field } from "../components/ui";
import type { Manager } from "../api/types";

// Static docs are served by the existing flexagg++ app (kept linking there per §8).
const DOC_LINKS = [
  { label: "Dashboard guide", href: "/flexagg++/app/static/guide.html" },
  { label: "Model & data reference", href: "/flexagg++/app/static/barra_model_reference.html" },
];

// One manager's label: entity_name (+ firm_type) when known, else the bare manager code — every
// field degrades silently when null (today's data has no entity attributes at all).
function managerLabel(m: Manager): string {
  if (!m.entity_name) return m.manager;
  return m.firm_type ? `${m.entity_name} · ${m.firm_type}` : m.entity_name;
}

// The manager control (multi-manager Phase 4). Tufte/Few: a control that cannot change anything
// is chartjunk — with a single manager (today's data) this renders as plain text, not a
// one-option dropdown. Only once a second manager actually exists does it become an interactive
// selector.
function ManagerField({ manager, managers, setManager }: {
  manager: string; managers: Manager[]; setManager: (m: string) => void;
}) {
  if (managers.length <= 1) {
    const m = managers[0];
    const label = m && m.manager === manager ? managerLabel(m) : manager;
    return (
      <Field label="Manager">
        <span className="num">{label}</span>
      </Field>
    );
  }
  return (
    <Field label="Manager">
      <select value={manager} onChange={(e) => setManager(e.target.value)}>
        {managers.map((m) => (
          <option key={m.manager} value={m.manager}>{managerLabel(m)}</option>
        ))}
      </select>
    </Field>
  );
}

export function ContextBar() {
  const { manager, date, scenario, dates, scenarioSets, managers, setManager, setDate, setScenario } = useApp();
  const [docsOpen, setDocsOpen] = useState(false);

  return (
    <div className="contextbar">
      {/* Not "<Manager> factor risk": the manager is named by ManagerField immediately to the
          right, so baking it into the title duplicated it, and hardcoding "Soros" went outright
          wrong once the multi-manager build put 124 managers behind the selector. */}
      <span className="title">Factor risk</span>
      <ManagerField manager={manager} managers={managers} setManager={setManager} />
      <Field label="As-of">
        <select value={date} onChange={(e) => setDate(e.target.value)} className="num">
          {dates.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
      </Field>
      <Field label="Scenario">
        <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
          {scenarioSets.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </Field>
      <span className="spacer" />
      <div style={{ position: "relative" }}>
        <button onClick={() => setDocsOpen((o) => !o)} title="Documentation">📖 Docs</button>
        {docsOpen && (
          <div
            style={{
              position: "absolute", right: 0, top: "2rem", background: "var(--bg)",
              border: "1px solid var(--line)", borderRadius: 2, padding: "0.4rem 0",
              zIndex: 20, minWidth: "14rem", boxShadow: "0 2px 6px rgba(0,0,0,0.06)",
            }}
            onMouseLeave={() => setDocsOpen(false)}
          >
            {DOC_LINKS.map((d) => (
              <a key={d.href} href={d.href} target="_blank" rel="noreferrer"
                style={{ display: "block", padding: "0.25rem 0.9rem", fontSize: 13 }}>
                {d.label}
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
