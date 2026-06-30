// The global context bar (docs/vite-ui-plan.md §9): book / as-of date / scenario-set shared by
// every lens, plus the docs (📖) menu. Few: shared context once, not repeated per-panel chrome.
import { useState } from "react";
import { useApp } from "../context/AppContext";
import { Field } from "../components/ui";

// Static docs are served by the existing flexagg++ app (kept linking there per §8).
const DOC_LINKS = [
  { label: "Dashboard guide", href: "/flexagg++/app/static/guide.html" },
  { label: "Model & data reference", href: "/flexagg++/app/static/barra_model_reference.html" },
];

export function ContextBar() {
  const { book, date, scenario, dates, scenarioSets, setBook, setDate, setScenario } = useApp();
  const [docsOpen, setDocsOpen] = useState(false);

  return (
    <div className="contextbar">
      <span className="title">Soros factor risk</span>
      <Field label="Book">
        <select value={book} onChange={(e) => setBook(e.target.value)}>
          <option value="Soros">Soros</option>
        </select>
      </Field>
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
