// Small shared UI atoms: a RAG dot, a query-state wrapper (loading/error/empty), a section header.
import { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { ragClass } from "../lib/format";
import type { BookMismatch } from "../api/types";

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

// True when a guarded single-book-artifact endpoint (/universe, /funnel, /span, /drift,
// /pnl_attribution*) came back with the book_mismatch status instead of its normal payload
// (risk_api.py's `_book_guard`, multi-manager Phase 3). Narrows the union so callers get the
// typed BookMismatch shape.
export function isBookMismatch(d: unknown): d is BookMismatch {
  return !!d && typeof d === "object" && (d as { status?: unknown }).status === "book_mismatch";
}

// The quiet, informational rendering of a book_mismatch: this view is single-book and was
// computed for a DIFFERENT book than the one currently selected. Deliberately NOT an alarm
// colour (this is an expected, disclosed state, not a failure) — same restrained "muted small"
// treatment the rest of the app uses for "insufficient data"/"no limits configured" states.
export function BookMismatchNotice({ m }: { m: BookMismatch }) {
  return (
    <p className="muted small" style={{ maxWidth: "46rem" }}>
      This view is computed for the <strong>{m.artifact_book ?? "—"}</strong> book only — not
      available for <strong>{m.requested_book}</strong>. {m.reason}
    </p>
  );
}

// QueryState + the book_mismatch check in one place: every guarded single-book lens (Universe,
// Funnel, Span, Drift, the PnL attribution tab) renders through this instead of QueryState
// directly, so a mismatch degrades to BookMismatchNotice rather than an empty chart, a spinner
// forever, or a crash trying to read fields the mismatch payload doesn't have.
export function GuardedQueryState<T>({
  q, children, empty,
}: { q: UseQueryResult<T | BookMismatch>; children: (data: T) => ReactNode; empty?: ReactNode }) {
  return (
    <QueryState q={q} empty={empty}>
      {(d) => (isBookMismatch(d) ? <BookMismatchNotice m={d} /> : children(d as T))}
    </QueryState>
  );
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
