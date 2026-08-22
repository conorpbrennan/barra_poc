// TanStack Query hooks over risk_api.py. The 5-min staleTime mirrors the Streamlit
// @st.cache_data(ttl=300): GETs cache, dedupe, and refetch on context change. Query keys carry
// every parameter so a context-bar change (manager/date/scenario) refetches the right slice.
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { apiGet, apiSend } from "./client";
import type {
  Meta, Dims, PivotResult, TrendsResult, LimitsResult, DqResult, BacktestResult,
  DrawdownResult, LiquidityResult, ReverseStressResult, UniverseResult, FunnelResult,
  SpanResult, DriftResult, WhatChangedResult, AttributionRow, WhatIfResult, Rec,
  PnlAttributionResult, PnlResidualResult, PnlLinkageResult, ContributionsResult,
  ValidationResult, RegressionResult, FactorCovResult,
  HedgeResult, ExposureProfileResult, FactorPortfolioResult, PnlNamesResult,
  ManagerMismatch, VarBridgeResult, PnlDrillPositionResult, PnlDrillFactorResult,
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

export function useTrends(set: string, measures: string, by?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["trends", set, measures, by ?? "", manager],
    queryFn: () => apiGet<TrendsResult>("/trends", { set, measures, by, manager }),
    ...common,
  });
}

export function useLimits(date: string, set: string, manager: string) {
  return useQuery({
    queryKey: ["limits", date, set, manager],
    queryFn: () => apiGet<LimitsResult>("/limits", { date, set, manager }),
    enabled: !!date,
    ...common,
  });
}

export function useDq() {
  return useQuery({ queryKey: ["dq"], queryFn: () => apiGet<DqResult>("/dq"), ...common });
}

export function useBacktest(set: string, date: string, manager: string) {
  return useQuery({
    queryKey: ["backtest", set, date, manager],
    queryFn: () => apiGet<BacktestResult>("/backtest", { set, date, manager }),
    enabled: !!date,
    ...common,
  });
}

export function useDrawdown(set: string, date: string, manager: string) {
  return useQuery({
    queryKey: ["drawdown", set, date, manager],
    queryFn: () => apiGet<DrawdownResult>("/drawdown", { set, date, manager }),
    enabled: !!date,
    ...common,
  });
}

export function useLiquidity(date: string, manager: string, participation: number, horizon: number) {
  return useQuery({
    queryKey: ["liquidity", date, manager, participation, horizon],
    queryFn: () => apiGet<LiquidityResult>("/liquidity", { date, manager, participation, horizon }),
    enabled: !!date,
    ...common,
  });
}

export function useReverseStress(loss: number | undefined, date: string, manager: string) {
  return useQuery({
    queryKey: ["reverse_stress", loss ?? null, date, manager],
    queryFn: () => apiGet<ReverseStressResult>("/reverse_stress", { loss, date, manager }),
    enabled: !!date,
    ...common,
  });
}

// These four read a SINGLE-MANAGER precomputed artifact (barra_universe_membership/funnel/span/
// drift.py) — risk_api.py's `_manager_guard` (multi-manager Phase 3) returns a `ManagerMismatch`
// payload, HTTP 200, instead of the normal shape when `manager` isn't verifiably the one the
// artifact covers. `manager` defaults to "Soros" to match the endpoints' own default exactly, so
// an omitted manager is byte-identical to pre-Phase-4 behaviour.
export function useUniverse(date?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["universe", date ?? "", manager],
    queryFn: () => apiGet<UniverseResult | ManagerMismatch>("/universe", { date, manager }),
    ...common,
  });
}
export function useFunnel(date?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["funnel", date ?? "", manager],
    queryFn: () => apiGet<FunnelResult | ManagerMismatch>("/funnel", { date, manager }),
    ...common,
  });
}
export function useSpan(date?: string, fx = "Size", fy = "ResidVol", manager = "Soros") {
  return useQuery({
    queryKey: ["span", date ?? "", fx, fy, manager],
    queryFn: () => apiGet<SpanResult | ManagerMismatch>("/span", { date, fx, fy, manager }),
    ...common,
  });
}
export function useDrift(split = "2021-01-01", manager = "Soros") {
  return useQuery({
    queryKey: ["drift", split, manager],
    queryFn: () => apiGet<DriftResult | ManagerMismatch>("/drift", { split, manager }),
    ...common,
  });
}

export function useWhatChanged(date?: string, prev?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["whatchanged", date ?? "", prev ?? "", manager],
    queryFn: () => apiGet<WhatChangedResult>("/whatchanged", { date, prev, manager }),
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
export function useWhatif(date: string, manager: string, trades: Trade[]) {
  return useQuery({
    queryKey: ["whatif", date, manager, JSON.stringify(trades)],
    queryFn: () => apiSend<WhatIfResult>("POST", "/whatif", { date, manager, trades }),
    enabled: !!date,
    ...common,
  });
}

// PnL attribution (Step 15). `from`/`to` empty strings mean the API default (trailing 12m).
// All four /pnl_attribution* routes carry the same single-manager artifact guard as
// useUniverse/useFunnel/useSpan/useDrift above (barra_pnl_attribution.py's precompute).
export function usePnlAttribution(from?: string, to?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["pnl_attribution", from ?? "", to ?? "", manager],
    queryFn: () => apiGet<PnlAttributionResult | ManagerMismatch>("/pnl_attribution", { from, to, manager }),
    ...common,
  });
}
export function usePnlResidual(from?: string, to?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["pnl_residual", from ?? "", to ?? "", manager],
    queryFn: () => apiGet<PnlResidualResult | ManagerMismatch>("/pnl_attribution/residual", { from, to, manager }),
    ...common,
  });
}
export function usePnlLinkage(horizon = 3, T?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["pnl_linkage", horizon, T ?? "", manager],
    queryFn: () => apiGet<PnlLinkageResult | ManagerMismatch>("/pnl_attribution/linkage", { horizon, T, manager }),
    ...common,
  });
}

export function useContributions(date: string, manager = "Soros") {
  return useQuery({
    queryKey: ["contributions", date, manager],
    queryFn: () => apiGet<ContributionsResult>("/contributions", { date, manager }),
    ...common,
  });
}

export function useCalibration(window = 24, manager = "Soros") {
  return useQuery({
    queryKey: ["calibration", window, manager],
    queryFn: () => apiGet<ValidationResult>("/calibration", { window, manager }),
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

export function useVarBridge(date: string, manager = "Soros", set = "HistFull") {
  return useQuery({
    queryKey: ["var_bridge", date, manager, set],
    queryFn: () => apiGet<VarBridgeResult>("/var_bridge", { date, manager, set }),
    enabled: !!date,
    ...common,
  });
}

export function useHedge(date: string, manager = "Soros") {
  return useQuery({
    queryKey: ["hedge", date, manager],
    queryFn: () => apiGet<HedgeResult>("/hedge", { date, manager }),
    ...common,
  });
}

export function useExposureProfile(factor: string, date: string, manager = "Soros") {
  return useQuery({
    queryKey: ["exposure_profile", factor, date, manager],
    queryFn: () => apiGet<ExposureProfileResult>("/exposure_profile", { factor, date, manager }),
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

export function usePnlNames(from?: string, to?: string, manager = "Soros") {
  return useQuery({
    queryKey: ["pnl_names", from ?? "", to ?? "", manager],
    queryFn: () => apiGet<PnlNamesResult | ManagerMismatch>("/pnl_attribution/names", { from, to, manager }),
    ...common,
  });
}

// Live per-manager reconcile drill (2026-08-22) — the Attribution reconcile drawers' replacement
// for the /pivot query on the manager-independent Factor contribution measure; computed from the
// frames for the requested manager directly, so it works on any loaded manager (no artifact guard).
export function usePnlDrillPosition(T: string, to: string, manager: string, position: string, enabled = true) {
  return useQuery({
    queryKey: ["pnl_drill_position", T, to, manager, position],
    queryFn: () => apiGet<PnlDrillPositionResult>("/pnl_attribution/drill", { T, to, manager, position }),
    enabled: enabled && !!T && !!to && !!position,
    ...common,
  });
}
export function usePnlDrillFactor(T: string, to: string, manager: string, factor: string, enabled = true) {
  return useQuery({
    queryKey: ["pnl_drill_factor", T, to, manager, factor],
    queryFn: () => apiGet<PnlDrillFactorResult>("/pnl_attribution/drill", { T, to, manager, factor }),
    enabled: enabled && !!T && !!to && !!factor,
    ...common,
  });
}

export function useExposures(date: string, manager = "Soros") {
  return useQuery({
    queryKey: ["exposures", date, manager],
    queryFn: () => apiGet<Rec[]>("/exposures", { date, manager }),
    enabled: !!date,
    ...common,
  });
}
