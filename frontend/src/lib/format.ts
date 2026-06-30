// Number formatting. Risk numbers are fractions of book value (0.035 = 3.5%); VaR/ES/vol are
// losses reported positive. Keep tabular alignment and don't over-state precision.

export function pct(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(dp)}%`;
}

export function signedPct(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = (v * 100).toFixed(dp);
  return `${v > 0 ? "+" : ""}${s}%`;
}

export function num(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(dp);
}

export function signedNum(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(dp)}`;
}

export function compact(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(0);
}

export function days(v: number | null | undefined, dp = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(dp)}d`;
}

// Map the various RAG vocabularies the API uses (limits: green/amber/breach; dq: pass/warn/fail;
// backtest: green/amber/red) to one CSS class + a glyph.
export function ragClass(status: string | null | undefined): string {
  switch ((status || "").toLowerCase()) {
    case "green": case "pass": case "ok": return "rag-green";
    case "amber": case "warn": return "rag-amber";
    case "red": case "fail": case "breach": return "rag-red";
    default: return "muted";
  }
}
export function ragLabel(status: string | null | undefined): string {
  const s = (status || "").toLowerCase();
  if (["green", "pass", "ok"].includes(s)) return "pass";
  if (["amber", "warn"].includes(s)) return "warn";
  if (["red", "fail", "breach"].includes(s)) return s === "breach" ? "breach" : "fail";
  return s || "—";
}
