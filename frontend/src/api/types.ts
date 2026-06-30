// Response shapes for risk_api.py. These mirror the JSON the endpoints emit (see CLAUDE.md and
// the docstrings in risk_api.py). Records are loosely typed (tidy dicts keyed by dim/measure name)
// because /pivot is generic; the typed fields are the structured endpoints.

export type Rec = Record<string, string | number | null>;

export interface Meta {
  dates: string[];
  scenario_sets: string[];
  factors: string[];
  ts_measures: string[];
  by_levels: string[];
}

export interface Dims {
  dimensions: string[];
  measures: string[];
  scenario_dependent: string[];
  members: Record<string, string[]>;
  dates: string[];
  scenario_sets: string[];
}

export interface PivotResult {
  rows: string[];
  cols: string[];
  measures: string[];
  totals: boolean;
  warning: string | null;
  records: Rec[];
  per_row?: Rec[];
  per_col?: Rec[];
  grand?: Record<string, number | null>;
}

export interface TrendsResult {
  set: string;
  measures: string[];
  by: string | null;
  records: Rec[];
}

export interface LimitCheck {
  name: string;
  scope: string;
  value: number | null;
  warn: number | null;
  limit: number | null;
  status: "green" | "amber" | "breach" | "unknown";
  headroom: number | null;
  detail: string | null;
}
export interface LimitsResult {
  date: string;
  set: string;
  book: string;
  status: "green" | "amber" | "breach" | "none";
  configured: boolean;
  checks: LimitCheck[];
  breaches: LimitCheck[];
}

export interface DqCheck { level: "PASS" | "WARN" | "FAIL"; name: string; detail: string }
export interface DqResult {
  status: "pass" | "warn" | "fail";
  summary: { PASS: number; WARN: number; FAIL: number };
  checks: DqCheck[];
  latest_date: Record<string, string | null>;
  stubs: { n_securities: number; sector_unknown: number; country_stub_US: number };
}

export interface BacktestResult {
  set: string; book: string; date: string; alpha: number; window: number;
  method: string; lam: number | null;
  status: "ok" | "insufficient";
  tested?: number; exceptions?: number; expected?: number; rate?: number | null;
  kupiec_LR?: number; kupiec_crit?: number; kupiec_reject?: boolean;
  basel_zone?: "green" | "amber" | "red" | "unknown";
  binom_cdf?: number | null;
  n_exception_dates?: number; exception_dates?: string[]; n?: number;
}

export interface DrawdownResult {
  set: string; book: string; date: string;
  status: "ok" | "insufficient";
  n?: number; max_drawdown?: number;
  peak_date?: string; trough_date?: string; drawdown_obs?: number;
  recovered?: boolean; recovery_date?: string | null; longest_underwater_obs?: number;
  path?: { date: string; equity: number; drawdown: number }[];
}

export interface WhatIfRisk {
  scenario_var_99: number; scenario_var_975: number;
  es_975: number; es_99: number; specific_vol: number;
  total_var_99: number; risk_hhi: number | null; gross: number; net: number;
}
export interface WhatIfResult {
  date: string; book: string;
  trades: { position: string; ticker: string; old: number; new: number }[];
  before: WhatIfRisk; after: WhatIfRisk; delta: Partial<WhatIfRisk>;
  holdings: { position: string; ticker: string; weight: number }[];
  universe: { position: string; ticker: string }[];
}

export interface LiquidityResult {
  date: string; book: string; participation: number; horizon_days: number;
  n_names: number; pct_mv_within_horizon: number | null; pct_weight_within_horizon: number;
  weighted_avg_days: number | null; max_days: number | null;
  n_no_adv: number; weight_no_adv: number;
  detail: Rec[]; no_adv_names: Rec[]; note: string;
}

export interface StressComponent {
  factor: string; exposure: number; sigma: number; vol: number;
  shock_return: number; pnl: number;
}
export interface StressResult {
  date: string; book: string; shocks: Record<string, number>;
  total_pnl: number; loss: number; components: StressComponent[];
}
export interface ReverseStressFactor {
  factor: string; exposure: number; vol: number;
  sigma_to_breach: number | null; abs_sigma: number | null;
}
export interface ReverseStressResult {
  date: string; book: string; loss: number;
  factors: ReverseStressFactor[]; weakest: ReverseStressFactor | null;
}

export interface UniverseResult {
  buckets: string[];
  series: Rec[];
  selected_date: string | null;
  latest: { report_date: string | null; n_names: number; split: Record<string, number>;
    outside_sp1500: number; unclassified: number };
  detail: Rec[];
  notes: string[] | string;
}

export interface FunnelResult {
  stages: string[];
  series: Rec[];
  selected_date: string | null;
  latest: Record<string, number | Record<string, number>>;
  dropped: Rec[];
  config: Record<string, unknown>;
  unavailable_stages: string[];
  note: string;
}

export interface SpanResult {
  factors: string[];
  series: { month: string; inside_wt: number; n_held: number; n_inside: number }[];
  selected_date: string | null;
  latest: Record<string, number | string>;
  detail: Rec[];
  scatter: {
    fx: string; fy: string;
    cloud: { x: number | null; y: number | null }[];
    held: { x: number | null; y: number | null; inside: boolean; issuer: string }[];
  };
  note: string;
}

export interface DriftResult {
  factors: string[];
  sources: string[];
  split: string; t0: string; t1: string;
  series: Rec[];
  summary: Rec[];
  note: string;
}

export interface WhatChangedResult {
  book: string; from: string; to: string;
  positions: {
    entered: { issuer: string; ticker: string; weight: number }[];
    exited: { issuer: string; ticker: string; weight: number }[];
    resized: { issuer: string; ticker: string; w0: number; w1: number; delta: number }[];
    n_entered: number; n_exited: number; n_before: number; n_after: number;
  };
  exposure_attribution: Rec[];
  risk: Record<string, { before: number | null; after: number | null; delta: number | null }>;
  note: string;
}

// /attribution returns tidy rows keyed by the level (Country/Sector/Issuer/Position) + measures;
// accessed by bracket, so a plain tidy record is the right shape.
export type AttributionRow = Rec;

// ---- saved views (views_api.py) ----
export interface ViewLeaf {
  name: string; slug: string; path: string; file: string;
  created?: string; updated?: string;
}
export interface ViewTree { folders: Record<string, ViewTree>; views: ViewLeaf[] }
export interface ViewDoc {
  schema_version: number; name: string; path: string;
  created: string; updated: string; state: ViewState;
}
export interface PivotQuery {
  name: string; rows: string[]; cols: string[]; measures: string[];
  filters?: Record<string, string[]>;
}
export interface ViewState {
  rows: string[]; cols: string[]; measures: string[];
  slice_dims?: string[]; filters?: Record<string, string[]>;
  row_tot?: boolean; col_tot?: boolean; as_pct?: boolean; hide_empty?: boolean;
  heat?: boolean; prec?: number; sort?: unknown;
  date_fmt?: string; render?: "grid" | "chart";
  queries?: PivotQuery[]; chart?: Record<string, unknown> | null;
}
