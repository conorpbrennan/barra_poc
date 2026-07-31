// TanStack Query hooks over risk_api.py. The 5-min staleTime mirrors the Streamlit
// @st.cache_data(ttl=300): GETs cache, dedupe, and refetch on context change. Query keys carry
// every parameter so a context-bar change (book/date/scenario) refetches the right slice.
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import type {
  Meta, Dims, PivotResult, TrendsResult, LimitsResult, DqResult, BacktestResult,
  DrawdownResult, LiquidityResult, ReverseStressResult, UniverseResult, FunnelResult,
  SpanResult, DriftResult, WhatChangedResult, AttributionRow, WhatIfResult, Rec,
  PnlAttributionResult, PnlResidualResult, PnlLinkageResult, ContributionsResult,
  ValidationResult, RegressionResult, FactorCovResult,
  HedgeResult, ExposureProfileResult, FactorPortfolioResult, PnlNamesResult,
  BookMismatch,
} from "./types";

export interface Trade { position: string; weight: number }

const FIVE_MIN = 5 * 60 * 1000;
const common = { staleTime: FIVE_MIN, gcTime: FIVE_MIN, placeholderData: keepPreviousData };

export function useMeta() {
  return useQuery({ queryKey: ["meta"], queryFn: () => apiGet<Meta>("/meta"), staleTime: Infinity });
}
export function useDims() {
  return useQuery({ queryKey: ["dims"], queryFn: () => apiGet<Dims>("/dims"), staleTime: Infinity });
}

export function usePivot(
  rows: string, cols: string, measures: string,
  filters?: string, totals = false, enabled = true,
) {
  return useQuery({
    queryKey: ["pivot", rows, cols, measures, filters ?? "", totals],
    queryFn: () => apiGet<PivotResult>("/pivot", { rows, cols, measures, filters, totals }),
    enabled: enabled && !!rows && !!measures,
    ...common,
  });
}

export function useTrends(set: string, measures: string, by?: string) {
  return useQuery({
    queryKey: ["trends", set, measures, by ?? ""],
    queryFn: () => apiGet<TrendsResult>("/trends", { set, measures, by }),
    ...common,
  });
}

export function useLimits(date: string, set: string, book: string) {
  return useQuery({
    queryKey: ["limits", date, set, book],
    queryFn: () => apiGet<LimitsResult>("/limits", { date, set, book }),
    enabled: !!date,
    ...common,
  });
}

export function useDq() {
  return useQuery({ queryKey: ["dq"], queryFn: () => apiGet<DqResult>("/dq"), ...common });
}

export function useBacktest(set: string, date: string, book: string) {
  return useQuery({
    queryKey: ["backtest", set, date, book],
    queryFn: () => apiGet<BacktestResult>("/backtest", { set, date, book }),
    enabled: !!date,
    ...common,
  });
}

export function useDrawdown(set: string, date: string, book: string) {
  return useQuery({
    queryKey: ["drawdown", set, date, book],
    queryFn: () => apiGet<DrawdownResult>("/drawdown", { set, date, book }),
    enabled: !!date,
    ...common,
  });
}

export function useLiquidity(date: string, book: string, participation: number, horizon: number) {
  return useQuery({
    queryKey: ["liquidity", date, book, participation, horizon],
    queryFn: () => apiGet<LiquidityResult>("/liquidity", { date, book, participation, horizon }),
    enabled: !!date,
    ...common,
  });
}

export function useReverseStress(loss: number | undefined, date: string, book: string) {
  return useQuery({
    queryKey: ["reverse_stress", loss ?? null, date, book],
    queryFn: () => apiGet<ReverseStressResult>("/reverse_stress", { loss, date, book }),
    enabled: !!date,
    ...common,
  });
}

// These four read a SINGLE-BOOK precomputed artifact (barra_universe_membership/funnel/span/
// drift.py) — risk_api.py's `_book_guard` (multi-manager Phase 3) returns a `BookMismatch`
// payload, HTTP 200, instead of the normal shape when `book` isn't verifiably the one the
// artifact covers. `book` defaults to "Soros" to match the endpoints' own default exactly, so an
// omitted book is byte-identical to pre-Phase-4 behaviour.
export function useUniverse(date?: string, book = "Soros") {
  return useQuery({
    queryKey: ["universe", date ?? "", book],
    queryFn: () => apiGet<UniverseResult | BookMismatch>("/universe", { date, book }),
    ...common,
  });
}
export function useFunnel(date?: string, book = "Soros") {
  return useQuery({
    queryKey: ["funnel", date ?? "", book],
    queryFn: () => apiGet<FunnelResult | BookMismatch>("/funnel", { date, book }),
    ...common,
  });
}
export function useSpan(date?: string, fx = "Size", fy = "ResidVol", book = "Soros") {
  return useQuery({
    queryKey: ["span", date ?? "", fx, fy, book],
    queryFn: () => apiGet<SpanResult | BookMismatch>("/span", { date, fx, fy, book }),
    ...common,
  });
}
export function useDrift(split = "2021-01-01", book = "Soros") {
  return useQuery({
    queryKey: ["drift", split, book],
    queryFn: () => apiGet<DriftResult | BookMismatch>("/drift", { split, book }),
    ...common,
  });
}

export function useWhatChanged(date?: string, prev?: string, book = "Soros") {
  return useQuery({
    queryKey: ["whatchanged", date ?? "", prev ?? "", book],
    queryFn: () => apiGet<WhatChangedResult>("/whatchanged", { date, prev, book }),
    ...common,
  });
}

export function useAttribution(date: string, set: string, by: string) {
  return useQuery({
    queryKey: ["attribution", date, set, by],
    queryFn: () => apiGet<AttributionRow[]>("/attribution", { date, set, by }),
    enabled: !!date,
    ...common,
  });
}

// /whatif is POST: empty trades bootstraps the editor (holdings + universe + before figures);
// non-empty returns before/after/delta. Used by the Overview (gross/net/HHI) and the What-if lens.
export function useWhatif(date: string, book: string, trades: Trade[]) {
  return useQuery({
    queryKey: ["whatif", date, book, JSON.stringify(trades)],
    queryFn: () => apiSend<WhatIfResult>("POST", "/whatif", { date, book, trades }),
    enabled: !!date,
    ...common,
  });
}

// PnL attribution (Step 15). `from`/`to` empty strings mean the API default (trailing 12m).
// All four /pnl_attribution* routes carry the same single-book artifact guard as
// useUniverse/useFunnel/useSpan/useDrift above (barra_pnl_attribution.py's precompute).
export function usePnlAttribution(from?: string, to?: string, book = "Soros") {
  return useQuery({
    queryKey: ["pnl_attribution", from ?? "", to ?? "", book],
    queryFn: () => apiGet<PnlAttributionResult | BookMismatch>("/pnl_attribution", { from, to, book }),
    ...common,
  });
}
export function usePnlResidual(from?: string, to?: string, book = "Soros") {
  return useQuery({
    queryKey: ["pnl_residual", from ?? "", to ?? "", book],
    queryFn: () => apiGet<PnlResidualResult | BookMismatch>("/pnl_attribution/residual", { from, to, book }),
    ...common,
  });
}
export function usePnlLinkage(horizon = 3, T?: string, book = "Soros") {
  return useQuery({
    queryKey: ["pnl_linkage", horizon, T ?? "", book],
    queryFn: () => apiGet<PnlLinkageResult | BookMismatch>("/pnl_attribution/linkage", { horizon, T, book }),
    ...common,
  });
}

export function useContributions(date: string, book = "Soros") {
  return useQuery({
    queryKey: ["contributions", date, book],
    queryFn: () => apiGet<ContributionsResult>("/contributions", { date, book }),
    ...common,
  });
}

export function useCalibration(window = 24, book = "Soros") {
  return useQuery({
    queryKey: ["calibration", window, book],
    queryFn: () => apiGet<ValidationResult>("/calibration", { window, book }),
    ...common,
  });
}

export function useRegression() {
  return useQuery({
    queryKey: ["regression"],
    queryFn: () => apiGet<RegressionResult>("/regression"),
    ...common,
  });
}

export function useFactorCov(date?: string) {
  return useQuery({
    queryKey: ["factor_cov", date ?? ""],
    queryFn: () => apiGet<FactorCovResult>("/factor_cov", { date }),
    ...common,
  });
}

export function useHedge(date: string, book = "Soros") {
  return useQuery({
    queryKey: ["hedge", date, book],
    queryFn: () => apiGet<HedgeResult>("/hedge", { date, book }),
    ...common,
  });
}

export function useExposureProfile(factor: string, date: string, book = "Soros") {
  return useQuery({
    queryKey: ["exposure_profile", factor, date, book],
    queryFn: () => apiGet<ExposureProfileResult>("/exposure_profile", { factor, date, book }),
    ...common,
  });
}

export function useFactorPortfolio(factor: string, date: string) {
  return useQuery({
    queryKey: ["factor_portfolio", factor, date],
    queryFn: () => apiGet<FactorPortfolioResult>("/factor_portfolio", { factor, date }),
    ...common,
  });
}

export function usePnlNames(from?: string, to?: string, book = "Soros") {
  return useQuery({
    queryKey: ["pnl_names", from ?? "", to ?? "", book],
    queryFn: () => apiGet<PnlNamesResult | BookMismatch>("/pnl_attribution/names", { from, to, book }),
    ...common,
  });
}

export function useExposures(date: string) {
  return useQuery({
    queryKey: ["exposures", date],
    queryFn: () => apiGet<Rec[]>("/exposures", { date }),
    enabled: !!date,
    ...common,
  });
}
