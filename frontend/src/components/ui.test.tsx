// Guarded single-book state (multi-manager Phase 4): a book_mismatch payload from /universe,
// /funnel, /span, /drift, /pnl_attribution* must render as a quiet informational note — never an
// empty chart, a spinner forever, or a crash trying to read fields the mismatch shape doesn't
// have. Pure-render — no API, no QueryClient (a hand-built UseQueryResult-shaped stub is enough
// since GuardedQueryState/QueryState only read isLoading/isError/data/error off it).
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import type { UseQueryResult } from "@tanstack/react-query";
import { GuardedQueryState, isBookMismatch, BookMismatchNotice } from "./ui";
import type { BookMismatch } from "../api/types";

const MISMATCH: BookMismatch = {
  status: "book_mismatch", kind: "funnel", requested_book: "TigerGlobal",
  artifact_book: "Soros", basis: "inferred from the live positions frame (exactly one Book present)",
  reason: "the funnel artifact was computed for the 'Soros' book, not 'TigerGlobal' — serving it "
    + "under another book's label would be silently wrong data, not just stale data",
};

interface Happy { note: string }
const HAPPY: Happy = { note: "all good" };

function stubQuery<T>(data: T | undefined, opts?: { isLoading?: boolean; isError?: boolean; error?: Error }) {
  return {
    data, isLoading: opts?.isLoading ?? false, isError: opts?.isError ?? false,
    error: opts?.error ?? null,
  } as unknown as UseQueryResult<T>;
}

describe("isBookMismatch", () => {
  it("recognizes the exact book_mismatch shape", () => {
    expect(isBookMismatch(MISMATCH)).toBe(true);
  });
  it("is false for a normal payload, null, and undefined", () => {
    expect(isBookMismatch(HAPPY)).toBe(false);
    expect(isBookMismatch(null)).toBe(false);
    expect(isBookMismatch(undefined)).toBe(false);
  });
});

describe("BookMismatchNotice", () => {
  it("names both the covered book and the requested one, and states the reason — no alarm class", () => {
    const { container, getByText } = render(<BookMismatchNotice m={MISMATCH} />);
    const strongs = [...container.querySelectorAll("strong")].map((s) => s.textContent);
    expect(strongs).toEqual(["Soros", "TigerGlobal"]);   // covered book, then requested book
    getByText(/serving it under another book's label would be silently wrong data/);
    // restrained typography: an informational state, not an error (never the red .err class)
    const p = container.querySelector("p")!;
    expect(p.className).toBe("muted small");
  });
});

describe("GuardedQueryState", () => {
  it("renders the informational notice instead of the happy-path children on a book_mismatch", () => {
    const q = stubQuery<Happy>(MISMATCH as unknown as Happy);
    const { getByText, queryByText } = render(
      <GuardedQueryState q={q}>{(d) => <div>{d.note}</div>}</GuardedQueryState>,
    );
    getByText(/not available for/);
    expect(queryByText("all good")).toBeNull();     // never falls through to the happy render
  });

  it("renders the happy-path children unchanged when the payload is normal", () => {
    const q = stubQuery<Happy>(HAPPY);
    const { getByText, queryByText } = render(
      <GuardedQueryState q={q}>{(d) => <div>{d.note}</div>}</GuardedQueryState>,
    );
    getByText("all good");
    expect(queryByText(/not available for/)).toBeNull();
  });

  it("still loads/errors like plain QueryState (no crash, no silent blank on missing data)", () => {
    const loading = stubQuery<Happy>(undefined, { isLoading: true });
    const { getByText } = render(
      <GuardedQueryState q={loading}>{(d) => <div>{d.note}</div>}</GuardedQueryState>,
    );
    getByText("loading…");
  });
});
