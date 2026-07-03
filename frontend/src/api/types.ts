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
  hypo_shocks?: Record<string, Record<string, number>>;
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
  model_vol_1d: number;               // the reference risk number (σ = √(x'Fx + w'Δw))
  scenario_var_99: number; scenario_var_975: number;
  es_975: number; es_99: number; specific_vol: number;
  total_var_99: number; top5_ctr_share: number | null; gross: number; net: number;
}
export interface WhatIfResult {
  date: string; book: string;
  trades: { position: string; ticker: string; old: number; new: number }[];
  before: WhatIfRisk; after: WhatIfRisk; delta: Partial<WhatIfRisk>;
  holdings: { position: string; ticker: string; weight: number }[];
  universe: { position: string; ticker: string }[];
  unpriced?: { position: string; ticker: string; weight: number }[]; // held, no loadings this date
  priced_weight?: number;
  source?: string;                    // "cube" (scenario branch) | "numpy_fallback"
  verification?: { max_abs_diff_vols: number; max_rel_diff_tails: number } | { error: string };
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
export interface ConditionalComponent {
  factor: string; exposure: number; implied_return: number;
  implied_sigma: number | null; pnl: number; shocked: boolean;
}
export interface StressResult {
  date: string; book: string; shocks: Record<string, number>;
  total_pnl: number; loss: number; components: StressComponent[];
  source?: string;                    // "cube" (StressShock simulation) | "numpy_fallback"
  verification?: { total_abs_diff: number; max_component_abs_diff: number } | { error: string };
  conditional?: { total_pnl: number; loss: number; components: ConditionalComponent[]; note: string };
  correlation_stress?: {
    vol_mult: number; rho_blend: number;
    base_vol_1d: number; stressed_vol_1d: number;
    base_var99_normal: number; stressed_var99_normal: number;
  };
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

// ---- PnL attribution (Step 15: /pnl_attribution, /pnl_attribution/residual, .../linkage) ----
export interface PnlSeriesPoint { date: string; market: number; style: number; specific: number; realized: number }
export interface PnlFactorRow {
  factor: string; avg_exposure: number | null; cum_factor_return: number;
  contribution: number; pct_of_total: number | null; t_stat: number | null;
}
export interface PnlAttributionResult {
  from: string; to: string; book: string; n_days: number;
  calendar: { min: string; max: string };
  headline: { realized_geometric: number; factor: number; specific: number; specific_share: number | null };
  linked: Record<string, number>;
  series: PnlSeriesPoint[];
  factors: PnlFactorRow[];
  coverage: { mean_priced_share: number | null; min_priced_share: number | null;
              unpriced: { name: string; weight: number }[] };
  by?: Rec[];
  note: string;
}
export interface PnlCheck { name: string; value: number; status: string; verdict: string; fmt: string }
export interface PnlResidualResult {
  from: string; to: string; book: string; n_months: number; status: string;
  checks: PnlCheck[];
  specific_share: number | null; explained_share: number | null;
  factor_regression: { r2: number | null; loadings: { factor: string; beta: number; t_stat: number }[] };
  factor_bias: { factor: string; bias: number; band: number | null }[];
  concentration: { hhi: number | null; top5_share: number | null; n: number };
  hit_rate: { names: number | null; months: number | null };
  note: string;
}
export interface PnlLinkageDriver {
  kind: string;                       // exposure_migration | factor_move | mixed | vol_underforecast
  migrated: boolean; ratio: number | null;
  z_window: number | null; factor_sigma: number | null; text: string;
}
export interface PnlLinkageRow {
  name: string; kind: string; exposure: number | null; risk_share: number | null;
  realized: number; sd_base: number; sd_stressed: number; z: number | null; verdict: string;
  exposure_window_avg?: number | null;
  driver?: PnlLinkageDriver;          // present only on rows outside the ±2σ base band
}
export interface PnlPositionDriver {
  kind: string;                       // weight_migration | specific_move | factor_move | mixed
  migrated: boolean; ratio: number | null;
  z_window: number | null; specific_share: number | null;
  top_factor?: string | null; hidden_beta?: boolean; text: string;
}
export interface BreachComovement {
  mean_corr: number; max_corr: number; max_pair: string[];
  n_names: number; n_pairs: number; n_obs: number;
  names: string[]; shared_sector: string | null;
  verdict: "common_thread" | "independent"; text: string;
}
export interface PnlLinkagePosition {
  name: string; position: string; weight: number; weight_window_avg: number | null;
  realized: number; factor_pnl: number; specific_pnl: number;
  sd_base: number; z: number; verdict: string;
  driver?: PnlPositionDriver;         // present only on rows outside the ±2σ base band
}
export interface PnlLinkageResult {
  T: string; to: string; horizon_months: number; n_days: number; book: string;
  stress: { vol_mult: number; rho_blend: number };
  book_total: PnlLinkageRow; rows: PnlLinkageRow[];
  positions: PnlLinkagePosition[];
  min_weight?: number;                // materiality floor on w(T) for the surprises table
  dust_excluded?: { n: number;
    names: { name: string; weight: number; z: number; verdict: string }[] };
  breach_comovement: BreachComovement | null;
  surprises: PnlLinkageRow[];
  note: string;
}

// ---- Euler risk contributions (/contributions) ----
export interface ContributionFactorRow {
  factor: string; exposure: number; ctv: number; pct_of_variance: number | null;
}
export interface ContributionPositionRow {
  position: string; ticker: string; weight: number; mcr: number; ctr: number;
  pct_of_vol: number | null;
}
export interface ContributionsResult {
  date: string; book: string;
  source?: string;                    // "cube" — served from the cube measures
  verification?: { vol_abs_diff: number; max_ctv_abs_diff: number; max_ctr_abs_diff: number };
  vol_1d: number; var99_normal: number;
  factor_variance: number; specific_variance: number; total_variance: number;
  factor_share: number | null; sum_ctr: number; sum_ctv: number;
  factors: ContributionFactorRow[]; positions: ContributionPositionRow[];
  note: string;
}

// ---- model trust (/validation, /regression, /factor_cov) ----
export interface ValidationSeries {
  bias: { date: string; b: number }[]; band: number;
  exceedance_2s: number | null; n_months: number;
}
export interface ValidationResult {
  window: number; book: string; expected_exceedance_2s: number;
  series: { book: ValidationSeries; specific: ValidationSeries };
  note: string;
}
export interface RegressionFactorRow {
  factor: string; pct_days_t_gt2: number; mean_abs_t: number; n_days: number;
}
export interface RegressionResult {
  from: string; to: string; n_days: number;
  r2_monthly: { date: string; r2: number }[];
  r2_mean: number;
  n_names: { min: number; median: number; max: number };
  factors: RegressionFactorRow[];
  note: string;
}
export interface FactorCovResult {
  date: string; n_days: number; n_days_recent: number; factors: string[];
  corr: number[][];
  vol_full: Record<string, number>; vol_recent: Record<string, number>;
  avg_abs_corr: { full: number; recent: number };
  note: string;
}

// ---- alignment views (/hedge, /exposure_profile, /factor_portfolio, /pnl_attribution/names) ----
export interface HedgeRow {
  factor: string; exposure: number; hedge_units: number;
  vol_after: number; vol_reduction: number;
}
export interface HedgeResult {
  date: string; book: string; vol_base: number; specific_vol: number;
  rows: HedgeRow[];
  market_hedge: { h_star: number; vol_after: number; vol_reduction: number } | null;
  note: string;
}
export interface ExposureProfileResult {
  factor: string; date: string; book: string; recipe: string; n_names: number;
  quantiles: Record<string, number>;
  hist: { x0: number; x1: number; n: number }[];
  beyond3: { n: number; share: number; names: { ticker: string; loading: number }[] };
  held: { ticker: string; weight: number; loading: number }[];
  note: string;
}
export interface FactorPortfolioResult {
  factor: string; date: string; fit_universe: string; n_names: number;
  gross_leverage: number; net: number; self_exposure: number; max_cross_exposure: number;
  longs: { ticker: string; weight: number }[];
  shorts: { ticker: string; weight: number }[];
  note: string;
}
export interface PnlNameRow {
  ticker: string; position: string; factor_pnl: number; specific_pnl: number; realized: number;
  months: number; sign_persistence: number | null; hit_rate: number | null;
}
export interface PnlNamesResult {
  from: string; to: string; book: string;
  winners: PnlNameRow[]; losers: PnlNameRow[]; note: string;
}

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
  // `chart` is a COMPLETE Vega-Lite spec, or a LIST of them (one per graph); each carries a `source`
  // naming the query in `queries` whose records feed it. Rendered verbatim (charts are not rebuilt).
  queries?: PivotQuery[]; chart?: VegaSpec | VegaSpec[] | null;
  description?: string;   // human note: what the view captures (shown in the Pivot description pane)
}
export type VegaSpec = Record<string, unknown>;
