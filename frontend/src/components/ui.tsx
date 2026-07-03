// Small shared UI atoms: a RAG dot, a query-state wrapper (loading/error/empty), a section header.
import { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { ragClass } from "../lib/format";

export function RagDot({ status, title }: { status: string | null | undefined; title?: string }) {
  return <span className={`dot ${ragClass(status)}`} title={title || status || ""} />;
}

// Render-prop wrapper: handles loading / error uniformly so lenses stay focused on the happy path.
export function QueryState<T>({
  q, children, empty,
}: { q: UseQueryResult<T>; children: (data: T) => ReactNode; empty?: ReactNode }) {
  if (q.isLoading && !q.data) return <div className="spin">loading…</div>;
  if (q.isError) return <div className="err small">error: {(q.error as Error)?.message ?? "failed"}</div>;
  if (!q.data) return <>{empty ?? <div className="muted small">no data</div>}</>;
  return <>{children(q.data)}</>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="field">
      <label>{label}</label>
      {children}
    </span>
  );
}

export function H2({ children }: { children: ReactNode }) {
  return <h2>{children}</h2>;
}

// "How to read this" — the per-lens explainer: a collapsed disclosure (details on demand, Tufte)
// holding a short reading guide: the formula, the conventions, the caveats. Keep it to a few
// sentences; the full treatment lives in the static docs.
export function HowToRead({ children }: { children: ReactNode }) {
  return (
    <details className="howto">
      <summary>How to read this</summary>
      <div>{children}</div>
    </details>
  );
}
