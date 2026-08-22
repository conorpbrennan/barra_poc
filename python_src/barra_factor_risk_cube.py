"""
barra_factor_risk_cube.py
=========================
Atoti factor-risk cube (two-block historical model) + unified scenario/stress layer.

Pairs with barra_build_frames.py. Seven input frames, plus one optional 8th (Phase 2):
  exposures        (Date, Position, Factor) -> Loading        <-- GRANULAR LEAF
  positions        (Date, Book, Position)   -> Weight, MV      <-- multi-manager 13F overlay
  securities       (Position)               -> Ticker, CIK, CUSIP, Issuer, Sector, Country
  factor_meta      (Factor)                 -> FactorGroup
  factor_returns   (Date, Factor)           -> Return          <-- source of every scenario set
  specific_var     (Date, Position)         -> SpecificVar     <-- diagonal block
  specific_returns (Date, Position)         -> SpecificReturn  <-- daily WLS residual (attribution);
                                                                    v2-only, optional
  managers         (Book)                   -> CIK, EntityName, FirmType, ETP-drop disclosure;
                                                                    optional, Phase 2 entity dim

ALL THREE SCENARIO MODES ARE ONE OPERATION:  dPnL = sum_k x_k * df_k
  x_k = aggregated factor exposure (the cube's "Net exposure" at the Factor level)
  df_k = a factor-shock vector that DIFFERS ONLY BY SOURCE:
     * HistFull      -> the full factor-return history          (historical simulation VaR/ES)
     * <event>       -> factor returns over a past window       (historical event replay)
     * Hypo:*        -> hand-set sigma-shocks, length-1 vector  (hypothetical stress)
The "one switch" is the ScenarioSet hierarchy: slice it, and the same measure gives that mode.
"""
from __future__ import annotations
import os
import pathlib
import time
import numpy as np
import pandas as pd
import atoti as tt

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"

# ---- build-stage timing (BARRA_CUBE_TIMINGS=1) --------------------------------------------
# Start-up was believed to be dominated by `read_pandas` of the 6M-row exposures table, but that
# was never attributed -- there was no per-stage clock inside build_cube. `_stage` is a no-op
# context manager unless the env flag is set (one dict write and a perf_counter pair otherwise),
# so it costs nothing in production. The last build's attribution is left on BUILD_TIMINGS for a
# harness/test to read without re-parsing stdout.
BUILD_TIMINGS: dict[str, float] = {}
_TIMINGS_ON = bool(os.environ.get("BARRA_CUBE_TIMINGS"))
_MARK_T0 = [0.0]


def _mark(name: str) -> None:
    """Close the stage that ended here: the time since the previous mark, keyed `name`.
    A single env check + two floats when off; `_mark_reset` opens the first stage."""
    if not _TIMINGS_ON:
        return
    now = time.perf_counter()
    dt = now - _MARK_T0[0]
    _MARK_T0[0] = now
    BUILD_TIMINGS[name] = BUILD_TIMINGS.get(name, 0.0) + dt
    print(f"    [stage] {name:34s} {dt:7.2f}s", flush=True)


# ---- bulk load: arrow cache + scheduling (2026-08-15, round 3) -------------------------------
# The four full-frame loads at the end of build_cube cost ~12 s of a ~27 s build. Attributed on
# a bare session (docs/cube-opt-round2-startup.md, round 3): ~45% is PYTHON -- atoti's
# `Table.load(DataFrame)` converts pandas -> arrow (1.5 s exposures, 2.8 s positions) and writes
# an arrow IPC file to a temp dir, THEN the JVM ingests that file; ~50% is the JVM ingest; the
# rest is cube-side indexing. Two things follow:
#   * The pandas -> arrow half is a pure function of the frame, identical on every restart. So it
#     is CACHED: the arrow file is written ONCE under data/.cube_cache/<table>.arrow, keyed on
#     the source parquet's path/mtime/size (stamped on the frame by load_frames) + row count +
#     columns, and the JVM ingests it directly (`ArrowLoad`) on the next start-up. Bytes on the
#     wire are identical to the uncached path (same converter, same writer -- atoti's own), so
#     the table contents cannot differ; a stale or missing entry falls back to the plain load.
#   * Concurrency across tables buys nothing measurable: `Table.load_async` is literally
#     `to_thread(self.load)`, and the JVM serialises the datastore transactions -- serial vs
#     threads measured 13.3 / 12.5 s and 17.8 / 16.9 s in back-to-back A/Bs (noise). Direct
#     `ParquetLoad` of cube-ready parquet was tried and REJECTED: the JVM parquet reader was
#     2x slower on Exposures and pathological on Positions (286 s, 11.6 M rows).
# BARRA_CUBE_LOAD=serial|threads|async picks the schedule (default serial -- the simplest thing
# that is not slower); BARRA_CUBE_ARROW_CACHE=0 disables the cache (default on).
_LOAD_MODE = os.environ.get("BARRA_CUBE_LOAD", "serial").lower()
_ARROW_CACHE_ON = os.environ.get("BARRA_CUBE_ARROW_CACHE", "1") not in ("0", "false", "no")
_ARROW_CACHE_DIR = OUT / ".cube_cache"


def _arrow_cache_key(table, df: pd.DataFrame) -> dict | None:
    """The staleness key: source parquet identity + the frame's shape. None = uncacheable."""
    src = df.attrs.get("source") if hasattr(df, "attrs") else None
    if not src or not isinstance(src, dict) or "mtime_ns" not in src:
        return None
    return {"table": table.name, "source": src, "n_rows": int(len(df)),
            "columns": [str(c) for c in df.columns],
            "dtypes": [str(t) for t in df.dtypes], "data_types": dict(table._data_types)}


def _unlink_quiet(p: pathlib.Path) -> None:
    """Best-effort delete. Cache housekeeping must never raise — an unlink that throws inside the
    write-failure handler escapes it and kills the build, which is exactly how a read-only
    /app/data took the notebook container down on 2026-08-21."""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _load_one(table, df: pd.DataFrame) -> str:
    """Load one frame into its table; returns how ("arrow-cache" | "arrow-cache-write" | "pandas").
    Cache path: read the JSON sidecar, compare the key, `ArrowLoad` the file. Miss: convert with
    atoti's own converter, write the IPC file into the cache, then load THAT file (so the first
    build after a rebuild pays what it always paid, plus one file write it now keeps)."""
    if not _ARROW_CACHE_ON:
        table.load(df); return "pandas"
    key = _arrow_cache_key(table, df)
    if key is None:
        table.load(df); return "pandas"
    try:
        import json
        from atoti._pandas_utils import pandas_to_arrow
        from atoti._arrow import write_arrow_to_file
        from atoti.data_load._arrow_load import ArrowLoad
    except Exception:                                   # private atoti API moved: no cache
        table.load(df); return "pandas"
    try:
        _ARROW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:                                     # read-only mount and no cache dir
        table.load(df); return "pandas"
    arrow_path = _ARROW_CACHE_DIR / f"{table.name}.arrow"
    meta_path = _ARROW_CACHE_DIR / f"{table.name}.json"
    try:
        if arrow_path.exists() and meta_path.exists() \
                and json.loads(meta_path.read_text()) == key:
            table.load(ArrowLoad(arrow_path))
            return "arrow-cache"
    except Exception as e:                              # unreadable/corrupt cache: rebuild it
        print(f"[cube] arrow cache for {table.name} unusable ({e!r}); regenerating", flush=True)
    if not os.access(_ARROW_CACHE_DIR, os.W_OK):        # e.g. the container's read-only /app/data:
        table.load(df); return "pandas"                 # read the cache, never try to write it
    try:
        for p in (arrow_path, meta_path):
            _unlink_quiet(p)
        arrow = pandas_to_arrow(df, data_types=table._data_types)
        tmp = arrow_path.with_suffix(".arrow.tmp")
        write_arrow_to_file(arrow, tmp)
        del arrow
        os.replace(tmp, arrow_path)
        table.load(ArrowLoad(arrow_path))
        meta_path.write_text(json.dumps(key))           # written LAST: no key without its file
        return "arrow-cache-write"
    except Exception as e:
        print(f"[cube] arrow cache write for {table.name} failed ({e!r}); plain load", flush=True)
        for p in (arrow_path, meta_path, arrow_path.with_suffix(".arrow.tmp")):
            _unlink_quiet(p)                            # MUST NOT raise: this is the handler
        table.load(df)
        return "pandas"


def _bulk_load(pairs: list) -> None:
    """Load every (table, frame) pair (see the note above). Whatever the schedule or cache state,
    each table ends up holding exactly its frame -- the modes differ only in scheduling and in
    where the arrow bytes come from, never in what lands in the table."""
    def _one(p):
        t, df = p
        _t0 = time.perf_counter()
        how = _load_one(t, df)
        if _TIMINGS_ON:
            print(f"    [stage]   load.bulk.{t.name:12s} {len(df):>10,} rows "
                  f"{time.perf_counter() - _t0:6.2f}s  ({how})", flush=True)
    if _LOAD_MODE == "serial" or len(pairs) < 2:
        for p in pairs:
            _one(p)
        return
    if _LOAD_MODE == "threads":
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(pairs)) as ex:
            list(ex.map(_one, pairs))
        return
    import asyncio, threading

    async def _all():
        await asyncio.gather(*[asyncio.to_thread(_one, p) for p in pairs])

    # build_cube may be called from inside a running event loop (uvicorn's lifespan), where
    # asyncio.run() is illegal -- so the gather runs on its own loop in a helper thread.
    err: list = []

    def _run():
        try:
            asyncio.run(_all())
        except Exception as e:                       # noqa: BLE001 -- fall back below
            err.append(e)
    th = threading.Thread(target=_run, name="barra-bulk-load"); th.start(); th.join()
    if err:
        print(f"[cube] concurrent bulk load failed ({err[0]!r}); loading serially", flush=True)
        for p in pairs:
            _one(p)


def _mark_reset() -> None:
    if _TIMINGS_ON:
        BUILD_TIMINGS.clear()
        _MARK_T0[0] = time.perf_counter()


class _DeferredMeasures:
    """Collect every measure definition + formatter, publish them in ONE batch.

    Why (measured, 2026-08-15): `cube.measures[name] = expr` goes through atoti's
    `Measures._update_delegate`, which distils the one definition and then calls
    `py4j_client.publish_measures(cube)` -- a FULL republish of the cube's measure DAG, PER
    MEASURE. And every `m["X"]` READ is a `find_measure` GraphQL round-trip, as is every
    `.formatter =`. On this cube that is ~55 republishes + ~120 round-trips = **21 s of a 57 s
    build**, none of it computing anything.

    `Measures.update({...})` distils the whole mapping and publishes ONCE, and
    `tt.mapping_lookup(check=False)` (public API) drops the lookup round-trip -- which also
    lets a definition REFERENCE a measure defined later in the same batch, since nothing is
    validated against the server until the flush. Insertion order is dependency order, so the
    distil pass resolves each reference against an already-distilled name.

    The DAG published is identical to the one-at-a-time build -- same definitions, same order,
    same formatters. Only the number of server round-trips changes.

    NOT everything can be deferred to a single batch: `tt.array.*` helpers call
    `check_array_type(measure)` -> `Measure.data_type`, which asks the server for the type of a
    measure that a pending batch has not published yet (`NoSuchElementException: No value
    present`). So the build calls `flush()` at the three points where an array helper takes a
    MEASURE (not an expression) as its argument -- `Scenario PnL vector`, `Scenario dates
    (epoch)`, `PIT Scenario PnL vector`. Four publishes instead of ~55; expressions built on
    OPERATIONS (`book_pnl_vec`, `_up`, `_shock_vec`, ...) never trip the check.
    """

    def __init__(self, measures):
        self._m = measures
        self.defs: dict = {}
        self.formats: dict[str, str] = {}

    def __setitem__(self, name: str, definition) -> None:
        self.defs[name] = definition

    def __getitem__(self, name: str):
        # A reference for use inside another definition. Unchecked: no server round-trip, and
        # forward references (to a measure later in this same batch) are legal.
        with tt.mapping_lookup(check=False):
            return self._m[name]

    def __contains__(self, name: str) -> bool:
        return name in self.defs

    def fmt(self, name: str, formatter: str) -> None:
        """Deferred `m[name].formatter = formatter` (each of those is its own mutation)."""
        self.formats[name] = formatter

    def flush(self) -> None:
        """Publish everything collected since the last flush (a no-op when nothing is pending)."""
        if not self.defs and not self.formats:
            return
        with tt.mapping_lookup(check=False):
            if self.defs:
                self._m.update(self.defs)
            for name, formatter in self.formats.items():
                self._m[name].formatter = formatter
        self.defs, self.formats = {}, {}
FRAME_NAMES = ["exposures", "positions", "securities", "factor_meta", "factor_returns", "specific_var"]
# specific_returns: v2-only (PnL attribution); v1 data degrades gracefully.
# managers: multi-manager entity metadata (Phase 2, keyed on Book); absent on any build before it
# existed (incl. all v1 data) -- degrades gracefully, no entity dimension, nothing else affected.
OPTIONAL_FRAMES = ["specific_returns", "managers",
                   # specific_pnl: the OPTIONAL precomputed (Date, Position) -> SpecPnL frame. Not
                   # written today; when a builder persists it (with FactorPnL on `exposures`),
                   # build_cube skips its pandas attribution prep entirely -- see Step 4 below.
                   "specific_pnl",
                   # stock_returns: the 9th (optional) frame (docs/price-var-plan.md) -- daily
                   # simple returns for every coverage name, v2-only like specific_returns.
                   # Absent on v1 data / any pre-Price-VaR build -> the Price family of measures
                   # is skipped entirely (has_price below), same degrade-cleanly pattern.
                   "stock_returns"]

# The Positions columns the CUBE needs: everything a measure reads, and nothing else. The frames
# dict keeps the full width (MV/ADV) for the API -- this list only bounds what crosses into the JVM.
POSITION_CUBE_COLS = ["Date", "Book", "Position", "Weight", "MV"]   # MV (dollars) since 2026-08-22

# Weight-unit measures (fractions of book value) that get a DOLLAR twin "<name> $" =
# measure × Book MV (the sliced book's 13F market value at the cell's Date). One list, shared
# with risk_api's allowlist and /pivot?units=dollar. Ratios (% of …, HHI, Top-5, sensitivity),
# variances, factor-unit measures, dates/counts and the book-independent attribution trio are
# deliberately NOT here — a dollar version of them is meaningless or wrong.
DOLLAR_MEASURES = [
    "Net exposure", "Net weight", "Gross weight",
    "Scenario VaR 95", "Scenario VaR 97.5", "Scenario VaR 99", "Scenario ES 97.5", "Scenario ES 99",
    "Scenario worst loss", "Scenario mean PnL", "Scenario PnL vol",
    "Total VaR 99", "Total ES 97.5", "Specific vol",
    "Marginal Scenario VaR 99", "Marginal Total VaR 99", "Marginal Scenario ES 97.5",
    "Incremental Scenario VaR 99", "Incremental Total VaR 99",
    "Model vol", "Marginal Model vol", "Incremental Model vol",
    "Vol ex factor", "Vol at min-variance hedge", "Stressed model vol", "Custom stress PnL",
    "PnL at day", "VaR line at day", "Worst pnl at day",
    # Price family (docs/price-var-plan.md) -- historical sim on raw stock returns, no factor
    # model. v2-only: absent when stock_returns.parquet wasn't built (has_price below); the
    # $-twin loop in build_cube skips any entry here that wasn't actually defined.
    "Price VaR 95", "Price VaR 97.5", "Price VaR 99", "Price ES 97.5", "Price ES 99",
    "Price worst loss", "Price mean PnL", "Price PnL vol",
    "Marginal Price VaR 99", "Incremental Price VaR 99", "Marginal Price ES 97.5",
]

# Historical event windows to replay (must fall inside the loaded sample; pre-2016 events need a
# longer factor-return history -- splice published style-factor returns for those windows).
EVENT_WINDOWS = {
    "Evt:COVID2020":   ("2020-02-01", "2020-05-31"),
    "Evt:Rates2022":   ("2022-01-01", "2022-10-31"),
    "Evt:Selloff2018": ("2018-10-01", "2018-12-31"),
}
# Hypothetical shocks in units of each factor's sigma; unlisted factors shocked 0.
HYPO_SHOCKS = {
    "Hypo:ValueRotation": {"Value": +2.0, "Momentum": -2.0},
    "Hypo:RiskOff":       {"Beta": -2.0, "ResidVol": +2.0, "Momentum": +1.0},
    "Hypo:MomentumCrash": {"Momentum": -3.0},
}


def load_frames(folder: pathlib.Path = OUT) -> dict[str, pd.DataFrame]:
    miss = [n for n in FRAME_NAMES if not (folder / f"{n}.parquet").exists()]
    if miss:
        raise FileNotFoundError(f"Run barra_build_frames.py first; missing: {miss}")
    frames = {n: pd.read_parquet(folder / f"{n}.parquet") for n in FRAME_NAMES}
    for n in OPTIONAL_FRAMES:
        if (folder / f"{n}.parquet").exists():
            frames[n] = pd.read_parquet(folder / f"{n}.parquet")
    for n, df in frames.items():
        # Provenance stamp for the bulk-load arrow cache (see _bulk_load): which parquet, and
        # its mtime/size at read time. `attrs` ride along through column selection / head();
        # a frame that lost them (any other construction) simply gets no cache -- never a wrong one.
        st = (folder / f"{n}.parquet").stat()
        # NAME, not the absolute path: the notebook container bind-mounts this folder at
        # /app/data, so a path-keyed entry never matches there — it would miss the cache the host
        # wrote and then try to rewrite a read-only mount. mtime_ns + size identify the file.
        df.attrs["source"] = {"name": f"{n}.parquet", "mtime_ns": st.st_mtime_ns,
                              "size": st.st_size}
    return frames


def _pit_month_ends(wide: pd.DataFrame, min_obs: int = 60) -> list[pd.Timestamp]:
    """The month-ends that get a PIT:* truncated-history set: every calendar month-end covered
    by the factor-return panel with at least `min_obs` prior trading days (a PIT set needs
    enough history for the sample estimators to mean anything)."""
    ends = pd.date_range(wide.index.min(), wide.index.max(), freq="ME")
    return [t for t in ends if (wide.index <= t).sum() >= min_obs]


def build_scenarios(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """One table keyed (ScenarioSet, Factor) -> ShockVec. Vectors share length WITHIN a set."""
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    vol = wide.std()
    rows = []
    # 1) full historical simulation
    for f in wide.columns:
        rows.append({"ScenarioSet": "HistFull", "Factor": f, "ShockVec": wide[f].to_numpy().tolist()})
    # 2) historical event replay
    for name, (a, b) in EVENT_WINDOWS.items():
        w = wide.loc[a:b]
        if len(w) == 0:
            continue
        for f in wide.columns:
            rows.append({"ScenarioSet": name, "Factor": f, "ShockVec": w[f].to_numpy().tolist()})
    # 3) hypothetical sigma-shocks (length-1 vectors)
    for name, shock in HYPO_SHOCKS.items():
        for f in wide.columns:
            rows.append({"ScenarioSet": name, "Factor": f,
                         "ShockVec": [float(shock.get(f, 0.0)) * float(vol[f])]})
    # NB the POINT-IN-TIME sets are NOT here — they live in their own table/hierarchy
    # (build_pit_scenarios below). See the note there.
    return pd.DataFrame(rows)


def build_pit_scenarios(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """POINT-IN-TIME sets: the factor-return history truncated at each month-end
    ("PIT:YYYY-MM-DD"), keyed (PITSet, Factor) -> ShockVec.

    These make the cube's risk measures honest as-of a date: model vol at (Date=t, PITSet=PIT:t)
    uses only information available at t — the full-history HistFull quietly uses later data when
    charted back in time. The LAST PIT set is the full panel == HistFull by construction.

    THEY ARE A SEPARATE TABLE AND HIERARCHY (2026-08-14, optimization Step 2). They used to be
    ~123 extra members of ScenarioSet, i.e. 95% of that hierarchy's members — and every query that
    groups BY ScenarioSet (the /dims member list, the notebook's set-comparison idiom, Risk HHI
    across sets) paid for all of them, though nothing ever enumerates a PIT set: they are only
    ever addressed BY NAME by the honest-vol path (`/pnl_attribution`'s `_pred_book_vols`).
    Measured: `Risk HHI` by all 130 sets FAILED at ~11 s; by the 7 real sets, 0.48 s.
    """
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    rows = []
    for t in _pit_month_ends(wide):
        w = wide.loc[:t]
        name = f"PIT:{t.date()}"
        for f in wide.columns:
            rows.append({"PITSet": name, "Factor": f, "ShockVec": w[f].to_numpy().tolist()})
    return pd.DataFrame(rows, columns=["PITSet", "Factor", "ShockVec"])


def build_scenario_days(scenarios: pd.DataFrame, scn_axis: pd.DataFrame) -> pd.DataFrame:
    """The SAME shocks as `build_scenarios`, one row per (set, factor, DAY) instead of one vector
    per (set, factor): (DaySet, Factor, Day) -> ShockAtDay, DayEpoch.

    Why a physical table (2026-08-15, optimization round 2). The per-day drill used to be served
    ONLY by the `ScenarioDay` parameter hierarchy, which reads `vector[member]` for each of its
    2,618 members: every member materialises the whole P&L vector and indexes it, so the book path
    cost ~10 s warm / ~25-50 s cold and +14 G of heap, and `ScenarioDay x Sector` failed outright.
    The cost was per-member evaluation machinery, not data volume — an 82-day event set measured
    the same as the 2,618-day HistFull.

    Exploded into facts, a day is an ordinary level: `PnL at day` is a plain additive sum-product
    (Net exposure x this day's shock) over the 23 factors, cacheable like any other aggregation,
    and day x Sector is a plain pivot. Derived from `scenarios`/`scn_axis` rather than recomputed
    from `factor_ret`, so element i of a set's ShockVec and Day=i are the same number BY
    CONSTRUCTION, not by two code paths agreeing. ~68k rows on the 7 real sets (the PIT sets get
    no day drill: they are the honest-vol path's plumbing, addressed by name, never unpacked).
    """
    axis = dict(zip(scn_axis["ScenarioSet"], scn_axis["DateVec"]))
    sets, factors, days, shocks, epochs = [], [], [], [], []
    for s, f, v in zip(scenarios["ScenarioSet"], scenarios["Factor"], scenarios["ShockVec"]):
        v = np.asarray(v, dtype="float64")
        dv = np.asarray(axis[s], dtype="int32")
        assert len(dv) == len(v), f"{s}/{f}: date axis {len(dv)} != shock vector {len(v)}"
        sets.append(np.full(len(v), s))
        factors.append(np.full(len(v), f))
        days.append(np.arange(len(v), dtype="int32"))
        shocks.append(v)
        epochs.append(dv)
    epoch = np.concatenate(epochs)
    return pd.DataFrame({"DaySet": np.concatenate(sets), "Factor": np.concatenate(factors),
                         "Day": np.concatenate(days),
                         # the calendar date as a KEY, so it is a LEVEL (a labelled x axis) rather
                         # than a measure: reading it off the axis costs no per-member aggregation,
                         # where `Date at day (epoch)` (a max over the joined column) measured
                         # roughly as much as the P&L measure itself.
                         "DayDate": pd.to_datetime(epoch, unit="D"),
                         "ShockAtDay": np.concatenate(shocks), "DayEpoch": epoch})


def build_scenario_axis(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """Per ScenarioSet, the ordered DATE AXIS of its shock/P&L vector, as epoch-day ints.

    Mirrors build_scenarios EXACTLY (same `wide`, same windows, same set membership) so that
    index i of a set's ShockVec/PnL vector maps to DateVec[i]. Hypo sets are length-1 vectors
    with no real date -> stamped with the last history date for alignment.
    """
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    epoch = pd.Timestamp("1970-01-01")
    def days(idx):
        return [int((pd.Timestamp(d) - epoch).days) for d in idx]
    rows = [{"ScenarioSet": "HistFull", "DateVec": days(wide.index)}]
    for name, (a, b) in EVENT_WINDOWS.items():
        w = wide.loc[a:b]
        if len(w):
            rows.append({"ScenarioSet": name, "DateVec": days(w.index)})
    last = days(wide.index[-1:])                       # length-1 stamp for hypothetical sets
    for name in HYPO_SHOCKS:
        rows.append({"ScenarioSet": name, "DateVec": last})
    # No PIT rows: the PIT sets are their own hierarchy (build_pit_scenarios) and the honest-vol
    # path reads scalars (vol) off them, never the per-day date dual.
    return pd.DataFrame(rows)


def build_price_axis(factor_ret: pd.DataFrame, style: list[str]) -> pd.DataFrame:
    """Per PriceSet, the date axis (epoch days) of its Price PnL vector — docs/price-var-plan.md.
    IDENTICAL sets/dates to build_scenario_axis's real sets (HistFull + every EVENT_WINDOWS
    window); Hypo:* sets are sigma-shocks on factors and have no Price mirror. A separate small
    table/function (not a reuse of ScenarioAxis) so PriceSet is a fully independent switch
    hierarchy, one-for-one with the ScenarioSet/Scenarios/ScenarioAxis trio."""
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    epoch = pd.Timestamp("1970-01-01")

    def days(idx):
        return [int((pd.Timestamp(d) - epoch).days) for d in idx]
    rows = [{"PriceSet": "HistFull", "DateVec": days(wide.index)}]
    for name, (a, b) in EVENT_WINDOWS.items():
        w = wide.loc[a:b]
        if len(w):
            rows.append({"PriceSet": name, "DateVec": days(w.index)})
    return pd.DataFrame(rows)


def build_price_returns(stock_ret: pd.DataFrame, factor_ret: pd.DataFrame,
                        style: list[str]) -> pd.DataFrame:
    """One table keyed (PriceSet, Position) -> ReturnVec, PricedVec — the Price family's raw
    material (docs/price-var-plan.md, step 2). Mirrors build_scenarios/build_scenario_axis
    EXACTLY: the SAME date axis per set (HistFull = the full daily factor-return calendar; Evt:*
    = the same EVENT_WINDOWS slices), so index i of a Price vector and index i of the matching
    Scenario vector are the same calendar day BY CONSTRUCTION. Hypo:* sets are sigma-shocks on
    factors and have no Price mirror.

    A name's daily return is SPARSE in `stock_ret` (barra_build_stock_returns.py / the builder's
    stock_returns_from_prices) — reindexing it onto each set's date axis ZERO-FILLS the missing
    days (no return that day; never backfilled from the model) and `PricedVec` flags which days
    were real (1.0) vs zero-filled (0.0) — the raw material for `Price coverage at tail`."""
    wide = (factor_ret[factor_ret["Factor"].isin(style)]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    full_idx = wide.index
    windows = {"HistFull": full_idx}
    for name, (a, b) in EVENT_WINDOWS.items():
        w = full_idx[(full_idx >= pd.Timestamp(a)) & (full_idx <= pd.Timestamp(b))]
        if len(w):
            windows[name] = w
    sr = stock_ret.pivot_table(index="Date", columns="Position", values="Return", aggfunc="last")
    rows = []
    for set_name, idx in windows.items():
        aligned = sr.reindex(idx)
        priced = aligned.notna()
        vals = aligned.fillna(0.0)
        for pos in vals.columns:
            rows.append({"PriceSet": set_name, "Position": pos,
                         "ReturnVec": vals[pos].to_numpy().tolist(),
                         "PricedVec": priced[pos].to_numpy().astype("float64").tolist()})
    return pd.DataFrame(rows, columns=["PriceSet", "Position", "ReturnVec", "PricedVec"])


def build_cube(frames: dict[str, pd.DataFrame], port: int = 9090):
    _mark_reset()
    exposures, positions = frames["exposures"], frames["positions"]
    securities, factor_meta = frames["securities"], frames["factor_meta"]
    factor_ret, specific = frames["factor_returns"], frames["specific_var"]

    style = [f for f in factor_ret["Factor"].unique() if f != "Market"]   # the 10 style factors (for reporting)
    # INCLUDE Market in the scenarios: it now carries a leaf loading of 1.0 per name (the v2
    # intercept), so the directional market factor return flows through dPnL. x_Market = Σ weights.
    # NB `.unique()` before the set(): `set(exposures["Factor"])` iterates 6M boxed Python strings
    # and measured ~3 s of the build for a 23-element answer.
    _exp_factors = set(exposures["Factor"].unique())
    scn_factors = [f for f in factor_ret["Factor"].unique() if f in _exp_factors or f == "Market"]
    _mark("prep.factor_lists")
    scenarios = build_scenarios(factor_ret, scn_factors)
    scn_axis = build_scenario_axis(factor_ret, scn_factors)   # date axis dual of the shock/P&L vectors
    scn_days = build_scenario_days(scenarios, scn_axis)       # the same shocks as day-grain FACTS
    _mark("prep.build_scenarios")
    pit_scn = build_pit_scenarios(factor_ret, scn_factors)    # truncated-history sets, own hierarchy
    _mark("prep.build_pit_scenarios")

    # Price family (docs/price-var-plan.md): v2-only, degrades like specific_returns when the 9th
    # frame is absent (v1 data / any pre-Price-VaR build).
    stock_ret = frames.get("stock_returns")
    has_price = stock_ret is not None and len(stock_ret) > 0
    if has_price:
        price_ret = build_price_returns(stock_ret, factor_ret, scn_factors)
        price_axis = build_price_axis(factor_ret, scn_factors)
        _mark("prep.build_price_returns")

    # Leaf products: `Net exposure` is now MEASURE-LEVEL (Loading x the JOINED Positions
    # Weight under an OriginScope) — benchmarked 2026-07-03 on atoti 0.9.15 at parity with the
    # old physical WLoading column on every query incl. the historical ~9s Date x Position
    # pivot (375ms vs 380ms), bit-exact. The point of the switch: the weight is read from the
    # Positions table AT QUERY TIME, so a source-scenario branch overriding Positions rows
    # flows through Net exposure and every measure chained off it (the what-if branch design —
    # docs/cube-measure-opportunities.md #4). The pandas WLoading below remains ONLY as an
    # intermediate for the attribution FactorPnL column (fwd returns baked at load; attribution
    # is deliberately NOT branch-sensitive). Unheld names: Weight join is None -> the product
    # contributes nothing, same as the old fillna(0) column.
    #
    # KNOWN LIMITATION (Phase 2, multi-manager): FactorPnL/SpecPnL below are BOOK-INDEPENDENT --
    # exactly as in the single-book (Soros-only) era -- because they are baked physical columns
    # on tables keyed WITHOUT Book (deliberately: attribution must stay immune to the what-if
    # trades branch, which lives on the Positions table's `scenarios[...]`, so these columns
    # cannot read Positions' live/branchable Weight at query time the way Net exposure does).
    # Making them genuinely per-book would need Book as a SECOND reused hierarchy dual on the
    # same side table alongside Factor (reused from Exposures) -- tried and reverted: every join
    # topology (single edge either way, or a two-edge "diamond" mapping Book via Positions AND
    # Factor via Exposures) left one of the two axes as an unresolvable ambiguous duplicate
    # hierarchy (`Disambiguate 'Book' to ... [('Positions','Book','Book'), ('FactorPnL','Book',
    # 'Book')]`), confirmed empirically, not merely suspected. `w` is deduped below to a SINGLE,
    # deterministic Weight per (Date, Position) (first book alphabetically) purely so the merge
    # can no longer silently create a duplicate-keyed row for a name held by more than one book
    # (a genuine data-corruption risk with the raw multi-book Positions frame) -- but the
    # resulting Factor contribution / Specific PnL / Realized PnL are NOT reliable for per-book
    # analysis when a name is held by more than one book; they read one arbitrary book's weight
    # for that name regardless of which Book is sliced. Disclosed, not silently fixed. Follow-up:
    # either confirm an Atoti-supported way to alias a table's column onto an EXISTING hierarchy
    # from a different table, or build N book-keyed tables in a loop over the active books.
    #
    # The pandas prep below runs on every restart (~seconds on the 124-book build). It is
    # SKIPPED when the frames already carry the derived columns -- `exposures.FactorPnL` and an
    # optional `specific_pnl` frame (Date, Position, SpecPnL). Nothing writes them today; this
    # is the forward-compatible half of optimization Step 4, so a builder that persists them
    # takes the work out of cube start-up without another cube change.
    prebuilt_factor_pnl = "FactorPnL" in exposures.columns
    prebuilt_spec_pnl = frames.get("specific_pnl")
    if not prebuilt_factor_pnl:
        w = (positions[["Date", "Position", "Book", "Weight"]]
             .sort_values(["Date", "Position", "Book"])
             .drop_duplicates(subset=["Date", "Position"], keep="first")
             [["Date", "Position", "Weight"]])
        exposures = exposures.merge(w, on=["Date", "Position"], how="left")
        exposures["WLoading"] = exposures["Loading"] * exposures["Weight"].fillna(0.0)
    _mark("prep.wloading_merge")

    # ---- PnL attribution (Step 15, v2-only): forward-month realized contributions -------------
    # Convention: the row at month-end d0 carries the PnL over the FOLLOWING month (d0, d1] — the
    # month the d0 exposures/weights explain (the regression fits days d0 < t <= d1 on the d0
    # loadings). Daily factor/specific returns are summed arithmetically into that window, so
    # `Factor contribution` is a plain additive column (WLoading x fwd-month factor return) that
    # foots at every level of Factor x Sector x Position, and `Specific PnL` is the as-of weight x
    # fwd-month residual per (Date, Position). Realized PnL = the two summed — an identity.
    spec_ret = frames.get("specific_returns")
    can_derive = spec_ret is not None and len(spec_ret) > 0 and not prebuilt_factor_pnl
    has_attribution = (prebuilt_factor_pnl and prebuilt_spec_pnl is not None) or can_derive
    spec_pnl = prebuilt_spec_pnl
    if can_derive:
        exp_dates = np.sort(pd.to_datetime(pd.Series(exposures["Date"].unique())).values)

        def _stamp_d0(s: pd.Series) -> pd.Series:
            """Largest exposure month-end STRICTLY BEFORE each daily date (the window owner)."""
            i = np.searchsorted(exp_dates, pd.to_datetime(s).values, side="left") - 1
            return pd.Series(np.where(i >= 0, exp_dates[np.clip(i, 0, None)], np.datetime64("NaT")),
                             index=s.index)

        fr_m = factor_ret.assign(D0=_stamp_d0(factor_ret["Date"])).dropna(subset=["D0"])
        fr_m = (fr_m.groupby(["D0", "Factor"], as_index=False)["Return"].sum()
                    .rename(columns={"D0": "Date", "Return": "FwdRet"}))
        exposures = exposures.merge(fr_m, on=["Date", "Factor"], how="left")
        exposures["FactorPnL"] = exposures["WLoading"] * exposures["FwdRet"].fillna(0.0)
        exposures = exposures.drop(columns="FwdRet")
        sr_m = spec_ret.assign(D0=_stamp_d0(spec_ret["Date"])).dropna(subset=["D0"])
        sr_m = (sr_m.groupby(["D0", "Position"], as_index=False)["SpecificReturn"].sum()
                    .rename(columns={"D0": "Date"}))
        spec_pnl = sr_m.merge(w, on=["Date", "Position"], how="inner")
        spec_pnl["SpecPnL"] = spec_pnl["Weight"] * spec_pnl["SpecificReturn"]
        spec_pnl = spec_pnl.loc[spec_pnl["SpecPnL"] != 0.0, ["Date", "Position", "SpecPnL"]]
    exposures = exposures.drop(columns=["Weight", "WLoading"], errors="ignore")   # prep-only
    _mark("prep.attribution_pandas")
    # NB: the same trick must NOT be applied to specific_var — it is a *joined* table, and a
    # plain SUM over a joined column fans out by fact-row multiplicity (each (Date, Position)
    # specvar row is reached once per factor leaf -> ~10x inflated variance, observed √10
    # jump in Specific vol). Its measure keeps the OriginScope form below.

    managers = frames.get("managers")
    has_managers = managers is not None and len(managers) > 0

    # Explicit JVM heap (2026-08-14, the 124-book Buyside expansion): with no -Xmx the JVM
    # defaults to 25% of RAM (~15.6g here), which the multi-book positions/exposures volume can
    # exhaust — the /trends OOM note dates from the 22k-name build. BARRA_CUBE_XMX overrides.
    xmx = os.environ.get("BARRA_CUBE_XMX", "32g")
    # Memory hygiene (optimization Step 5): a heavy query burst drives this heap to ~41 G and G1
    # gives it back slowly -- the production cube has twice needed a manual bounce after a demo.
    # -Xms2g starts small instead of committing a big heap up front, and G1PeriodicGCInterval
    # (5 min) makes G1 run a concurrent cycle while IDLE, so the burst is released between
    # sessions rather than held until the next allocation pressure.
    # -Xms is the measured optimum, not an assumption: -Xms12g was tried on the theory that the
    # 17.5M-row load would stop expanding the heap, and came out WORSE (40.4 s vs 38.8 s at that
    # point in the programme). BARRA_CUBE_XMS overrides it. Application class-data sharing
    # (`-XX:+AutoCreateSharedArchive`, JDK 21) was also tried against `Session.start`'s 5.3 s and
    # REJECTED: this jdk4py runtime never writes the archive (verified down to a bare
    # `java -version`; the JDK's own base archive is already mapped), and session.start measured
    # 5.29 s -> 5.30 s. See docs/cube-opt-round2-startup.md.
    xms = os.environ.get("BARRA_CUBE_XMS", "2g")
    session = tt.Session.start(tt.SessionConfig(port=port, java_options=[
        f"-Xmx{xmx}", f"-Xms{xms}", "-XX:G1PeriodicGCInterval=300000"]))
    _mark("session.start")
    # ^ port pinned so the UI URL survives restarts
    #
    # ---- MODEL FIRST, DATA LAST (2026-08-15) -------------------------------------------------
    # The three big tables are created from a SEED of their first `SEED_ROWS` rows, and the full
    # frames are loaded at the very END of build_cube, after every join, hierarchy, parameter
    # dimension and measure exists. Measured on this build: joins 3.9 s -> 0.3 s, hierarchy
    # creation 6.8 s -> 0.4 s, create_cube 0.8 s -> 0.4 s, because each of those steps re-indexes
    # / refreshes whatever the tables already hold; the bulk load that follows costs only ~2 s
    # more than the up-front loads did. Net ~9 s off the build.
    #
    # Seeding (rather than `session.create_table` with a hand-written schema) keeps atoti's own
    # pandas type inference as the ONE source of the schema -- and inference reads the frame's
    # DTYPES, not its values, so `head()` infers exactly what the full frame would. The tables are
    # KEYED, so re-loading the seed rows inside the full frame upserts them; no duplicates. The
    # loaded columns are all non-null (checked: exposures/positions/specific_var/specific_pnl have
    # zero NaN in every column the cube reads), so no seed/full nullability mismatch is possible.
    # Small tables (securities, factor_meta, scenarios, the PIT sets, managers) are loaded whole
    # up front -- together they cost ~1.5 s and several of them back a hierarchy.
    SEED_ROWS = 1000
    t_exp = session.read_pandas(exposures.head(SEED_ROWS),
                                keys={"Date", "Position", "Factor"}, table_name="Exposures")
    # SLIM (optimization Step 4): only the columns a measure reads go into the JVM. MV and ADV are
    # never read by any measure -- /liquidity and the what-if editor read them from the pandas
    # frames on S["frames"], which keep their full width. Measured: 6.6s -> 5.0s on this table.
    positions_cube = positions[POSITION_CUBE_COLS]
    t_pos = session.read_pandas(positions_cube.head(SEED_ROWS),
                                keys={"Date", "Book", "Position"},               table_name="Positions")
    t_sv  = session.read_pandas(specific.head(SEED_ROWS),
                                keys={"Date", "Position"},                       table_name="SpecificVar")
    _mark("load.seeds")
    t_sec = session.read_pandas(securities, keys={"Position"},                   table_name="Securities")
    t_fm  = session.read_pandas(factor_meta, keys={"Factor"},                    table_name="FactorMeta")
    _mark("load.Securities+FactorMeta")
    t_scn = session.read_pandas(scenarios,  keys={"ScenarioSet", "Factor"},      table_name="Scenarios")
    t_axis = session.read_pandas(scn_axis,  keys={"ScenarioSet"},                table_name="ScenarioAxis")
    _mark("load.Scenarios+Axis")
    t_days = session.read_pandas(scn_days, keys={"DaySet", "Factor", "Day", "DayDate"},
                                                                                 table_name="ScenarioDays")
    _mark("load.ScenarioDays")
    t_pit = (session.read_pandas(pit_scn, keys={"PITSet", "Factor"}, table_name="PITScenarios")
             if len(pit_scn) else None)
    _mark("load.PITScenarios")
    t_sr = (session.read_pandas(spec_pnl.head(SEED_ROWS), keys={"Date", "Position"},
                                table_name="SpecificPnL")
            if has_attribution else None)
    _mark("load.SpecificPnL")
    t_mgr = (session.read_pandas(managers, keys={"Book"}, table_name="Managers")
             if has_managers else None)
    _mark("load.Managers")
    # Price family tables (docs/price-var-plan.md): StockReturns mirrors the "big table, seeded
    # then bulk-loaded at the end" pattern (its array columns are comparable in bulk to
    # SpecificPnL's); PriceAxis is tiny like ScenarioAxis, loaded whole.
    t_price = (session.read_pandas(price_ret.head(SEED_ROWS), keys={"PriceSet", "Position"},
                                   table_name="StockReturns")
               if has_price else None)
    _mark("load.StockReturns_seed")
    t_paxis = (session.read_pandas(price_axis, keys={"PriceSet"}, table_name="PriceAxis")
               if has_price else None)
    _mark("load.PriceAxis")

    t_exp.join(t_pos, (t_exp["Date"] == t_pos["Date"]) & (t_exp["Position"] == t_pos["Position"]))
    t_exp.join(t_sec, t_exp["Position"] == t_sec["Position"])
    t_exp.join(t_fm,  t_exp["Factor"] == t_fm["Factor"])
    t_exp.join(t_sv,  (t_exp["Date"] == t_sv["Date"]) & (t_exp["Position"] == t_sv["Position"]))
    if t_sr is not None:
        t_exp.join(t_sr, (t_exp["Date"] == t_sr["Date"]) & (t_exp["Position"] == t_sr["Position"]))
    # PARTIAL join: map Factor only -> ScenarioSet becomes a hierarchy (the "switch"). One ShockVec
    # is selected per (ScenarioSet, Factor); exposures fan across sets but Net exposure is unaffected.
    t_exp.join(t_scn, t_exp["Factor"] == t_scn["Factor"])
    t_scn.join(t_axis, t_scn["ScenarioSet"] == t_axis["ScenarioSet"])   # date axis, one row per set
    # The day-grain twin of that partial join, DIRECTLY off the fact table (see build_scenario_days).
    # Its two un-mapped keys auto-create the `DaySet` and `Day` hierarchies, and nothing else in the
    # cube reads this table -- the existing scenario topology (and every measure on it) is untouched.
    # Deliberately NOT hung off Scenarios on (ScenarioSet, Factor), which would have reused the
    # existing set hierarchy: atoti 0.9.15 silently drops such a table from the cube's datastore
    # selection ("ShockAtDay is not part of the cube's datastore selection"), whichever order the
    # joins are declared in -- a second-hop join may not introduce a new key. Hanging Scenarios off
    # ScenarioDays instead DOES work and keeps one set hierarchy, but then every existing vector
    # measure reads its ShockVec through a 2,618x day fan-out: not worth the risk to the fast paths.
    t_exp.join(t_days, t_exp["Factor"] == t_days["Factor"])
    if t_pit is not None:
        # The SAME partial-join trick, second copy: PITSet is its own switch hierarchy, so the
        # ~123 truncated-history sets are off ScenarioSet and every "group by ScenarioSet" costs
        # 7 members instead of 130. Only the honest-vol measures are mirrored onto it (below).
        t_exp.join(t_pit, t_exp["Factor"] == t_pit["Factor"])
    if t_mgr is not None:
        # Entity metadata for the Book dimension (Phase 2). PARTIAL join on Book only, off the
        # SAME Positions table whose un-mapped Book key already makes Book a hierarchy -- so this
        # degrades cleanly when managers.parquet is absent (v1 data / any pre-Phase-2 build has
        # no entity dimension, nothing else affected).
        t_pos.join(t_mgr, t_pos["Book"] == t_mgr["Book"])
    if t_price is not None:
        # Second copy of the Scenario partial-join trick: PriceSet becomes its own switch
        # hierarchy off the un-mapped key -- but mapping POSITION only (Price PnL has no Factor
        # structure at all; Scenarios map Factor). Exposures fan across PriceSets, unaffected.
        t_exp.join(t_price, t_exp["Position"] == t_price["Position"])
        t_price.join(t_paxis, t_price["PriceSet"] == t_paxis["PriceSet"])
    _mark("joins")

    cube = session.create_cube(t_exp, mode="manual")
    _mark("create_cube")
    h, l = cube.hierarchies, cube.levels
    # Every `m[...] = ...` below is COLLECTED, not published -- one `update()` at the end of
    # build_cube publishes the lot (see _DeferredMeasures). Formatters go through `m.fmt(name,
    # spec)` for the same reason. Definitions and their order are unchanged.
    m = _DeferredMeasures(cube.measures)

    # Atoti guards a query's accumulated point count; the defaults are intermediate 1,000,000 /
    # transient 10,000,000. The single-book PoC never came near them, but the multi-manager build
    # (exposures 1.6M -> 5.7M leaf rows across 11 books) trips the intermediate limit on ordinary
    # queries -- /dims 500'd on EVERY dimension with
    #   RetrievalResultSizeException [points size before contribution = 1000000, max limit = 1000000]
    # taking the whole Pivot lens down with it. These are guards against runaway memory, not
    # correctness limits, so they are raised to keep the same headroom-to-universe ratio as before
    # (cube RSS measured at 1.1 GB on this dataset, against 62 GB on the box).
    cube.shared_context["queriesResultLimit.intermediateLimit"] = 20_000_000
    cube.shared_context["queriesResultLimit.transientLimit"] = 200_000_000
    # 124-book expansion (2026-08-14): an all-books query (Book on rows, scenario measures) is 123
    # per-book P&L vectors in one plan and blows ActivePivot's 30s default; single-book slices run
    # ~3s. Raised so cross-book comparison queries can complete rather than 500.
    cube.shared_context["queriesTimeLimit"] = 120

    # ONE `update()` for every explicit hierarchy: `h[name] = ...` runs its own GraphQL mutation
    # batch and a cube refresh per call, so the four (five with Manager) assignments cost five
    # refreshes. Same hierarchies, same levels -- one round of mutations.
    #   Security     — the browse dimension.
    #   PositionRank — FLAT position hierarchy for GLOBAL cross-member ranking (tt.rank ranks
    #                  siblings; under Security a position's siblings are its issuer's positions).
    #                  Level named PositionR so the plain l["Position"] lookups stay unambiguous;
    #                  hidden — rank plumbing, not a browse dim.
    #   Manager      — entity dimension (Phase 2): a SEPARATE hierarchy, not extra levels grafted
    #                  onto "Book", so the pre-existing auto-created single-level "Book" hierarchy
    #                  (and every filter/level lookup against it elsewhere, incl. risk_api.py) is
    #                  untouched. Manager is 1:1 with Book (same t_pos->t_mgr row), so filtering by
    #                  either stays consistent. It's never passed to a lift/OriginScope call,
    #                  exactly like Book itself -- so it always reads whatever book is currently
    #                  sliced, and is blank/ambiguous at the no-book grand total (same as any other
    #                  book-scoped attribute; see the grand-total note near Net exposure).
    _hiers = {
        "Security": {"Country": t_sec["Country"], "Sector": t_sec["Sector"],
                     "Issuer": t_sec["Issuer"], "Position": t_exp["Position"]},
        "PositionRank": {"PositionR": t_exp["Position"]},
        "FactorDim": {"FactorGroup": t_fm["FactorGroup"], "Factor": t_exp["Factor"]},
        "Date": {"Date": t_exp["Date"]},
    }
    if t_mgr is not None:
        _hiers["Manager"] = {"FirmType": t_mgr["FirmType"], "EntityName": t_mgr["EntityName"],
                             "CIK": t_mgr["CIK"]}
    _mark("hierarchies.build_spec")
    h.update(_hiers)
    _mark("hierarchies.update")
    try:
        h["PositionRank"].visible = False
    except Exception:
        pass
    # Book and ScenarioSet are NOT created manually: they are the un-mapped key columns of the
    # partial joins (Positions, Scenarios). In manual cube mode atoti auto-creates their
    # hierarchies once the mapped key columns (Date, Position, Factor) have hierarchies.
    _mark("hierarchies.hide_rank")
    _hier_names = {n for _, n in h}
    assert {"Book", "ScenarioSet"} <= _hier_names, sorted(_hier_names)
    if t_pit is not None:
        assert "PITSet" in _hier_names, sorted(_hier_names)
    if has_price:
        assert "PriceSet" in _hier_names, sorted(_hier_names)
    _mark("hierarchies.assert")

    # ---- additive exposures (drill/slice; independent of ScenarioSet) -------------------------
    # MEASURE-LEVEL product of the leaf Loading and the JOINED Positions Weight (see the leaf-
    # products note at the top of build_cube): reads the weight at query time, so scenario
    # branches on Positions flow through this and every chained measure. Benchmarked at parity
    # with the old physical column.
    m["Net exposure"] = tt.agg.sum(
        tt.agg.single_value(t_exp["Loading"]) * tt.agg.single_value(t_pos["Weight"]),
        scope=tt.OriginScope({l["Date"], l["Position"], l["Factor"]}))
    m.fmt("Net exposure", "DOUBLE[0.000]")

    # ---- entity dimension measures (Phase 2): the managers.parquet disclosure fields, read at
    # whatever Book is currently sliced (single_value over the Book-keyed Managers table joined
    # via Positions -- see the Manager hierarchy note above). Blank/ambiguous with no Book slice.
    if t_mgr is not None:
        m["Manager ETP dropped value share"] = tt.agg.single_value(t_mgr["dropped_value_share_latest"])
        m.fmt("Manager ETP dropped value share", "DOUBLE[0.00%]")
        m["Manager n filings"] = tt.agg.single_value(t_mgr["n_filings"])
        m["Manager n positions"] = tt.agg.single_value(t_mgr["n_positions_distinct"])

    # ---- ONE scenario engine: aggregate exposure to K factor scalars, scale each factor's shock
    #      vector once, sum K vectors -> P&L vector for the sliced ScenarioSet. (Confirm tt.array.*
    #      helper names in your SDK; validate the OriginScope pins step-2 to one vector per Factor.)
    #      NB: query risk measures sliced to a SINGLE ScenarioSet -- vector lengths differ across sets.
    m["Scenario PnL vector"] = tt.agg.sum(
        m["Net exposure"] * tt.agg.single_value(t_scn["ShockVec"]),
        scope=tt.OriginScope({l["Factor"]}))
    # ---- index -> date DUAL of the P&L vector: aligned 1:1 with "Scenario PnL vector". --------
    # DateVec[i] is the date that produced PnL vector[i] (epoch days; one row per set, so
    # single_value is unambiguous when sliced to a single ScenarioSet -- the same constraint the
    # vector measures carry). Lets a caller label the historical-sim / event-replay path and name
    # the worst-loss / VaR-breach dates. Defined HERE, next to the vector it indexes, so that the
    # one early publish below covers every array-typed measure (see _DeferredMeasures).
    m["Scenario dates (epoch)"] = tt.agg.single_value(t_axis["DateVec"])
    # ---- the PIT mirror's vector: the SAME engine driven by the PITSet switch. Its two scalar
    # measures stay down with Model vol; only the vector is hoisted, for the same publish reason.
    if t_pit is not None:
        m["PIT Scenario PnL vector"] = tt.agg.sum(
            m["Net exposure"] * tt.agg.single_value(t_pit["ShockVec"]),
            scope=tt.OriginScope({l["Factor"]}))
    # ---- Price family's raw vectors: second copy of the leaf-product idiom, but PER POSITION
    # (Price PnL has no Factor structure at all) — the same OriginScope({Date, Position}) trick
    # `Specific variance` uses to avoid fanning out across the 23 factor rows per (Date, Position)
    # in t_exp. `Price coverage vector` is the identical sum with PricedVec in place of ReturnVec
    # (the weight-share that was ACTUALLY priced that day, zero-fill excluded).
    if has_price:
        m["Price PnL vector"] = tt.agg.sum(
            tt.agg.single_value(t_pos["Weight"]) * tt.agg.single_value(t_price["ReturnVec"]),
            scope=tt.OriginScope({l["Date"], l["Position"]}))
        m["Price coverage vector"] = tt.agg.sum(
            tt.agg.single_value(t_pos["Weight"]) * tt.agg.single_value(t_price["PricedVec"]),
            scope=tt.OriginScope({l["Date"], l["Position"]}))
        m["Price dates (epoch)"] = tt.agg.single_value(t_paxis["DateVec"])
    # THE ONE EARLY PUBLISH. Every `tt.array.*` helper type-checks a MEASURE argument against the
    # server (`Measure.data_type`), so the array-typed measures above must exist before the
    # expressions that consume them. Everything else -- ~50 measures -- goes in the final flush.
    m.flush()
    _mark("measures.flush_vectors")
    m["Scenario mean PnL"]   = tt.array.mean(m["Scenario PnL vector"])     # hypo: the shock P&L; hist: ~0
    m["Scenario VaR 99"]     = -tt.array.quantile(m["Scenario PnL vector"], 0.01)
    m["Scenario worst loss"] = -tt.array.min(m["Scenario PnL vector"])     # worst single scenario
    for k in ("Scenario mean PnL", "Scenario VaR 99", "Scenario worst loss"):
        m.fmt(k, "DOUBLE[0.00%]")
    m["Scenario n"] = tt.array.len(m["Scenario PnL vector"])   # THIS set's vector length
    _mark("measures.scenario_core")

    # ---- extra confidence levels + Expected Shortfall (coherent tail measure) ------------------
    # VaR at 95 / 97.5 alongside the existing 99 (the 95/99 pair reads tail fatness; 97.5 is the
    # FRTB regulatory point). ES (a.k.a. CVaR) is the MEAN loss in the tail BEYOND VaR -- coherent
    # / sub-additive where VaR is not, and the Basel FRTB replacement for VaR. The tail SIZE k
    # scales with each set's OWN vector length n (HistFull 2203 vs COVID 82 vs hypo 1), so k is a
    # MEASURE: k = ceil(alpha * n) worst observations and ES = -mean of the k lowest P&L. n_lowest
    # accepts a measure for n, so one definition serves every ragged scenario set (k >= 1 always:
    # ceil of a positive number; k <= n since alpha < 1, so never indexes past the vector).
    m["Scenario VaR 95"]   = -tt.array.quantile(m["Scenario PnL vector"], 0.05)
    m["Scenario VaR 97.5"] = -tt.array.quantile(m["Scenario PnL vector"], 0.025)
    _k975 = tt.math.ceil(0.025 * m["Scenario n"])     # tail size for 97.5% ES
    _k99  = tt.math.ceil(0.01  * m["Scenario n"])     # tail size for 99% ES
    m["Scenario ES 97.5"] = -tt.array.mean(tt.array.n_lowest(m["Scenario PnL vector"], _k975))
    m["Scenario ES 99"]   = -tt.array.mean(tt.array.n_lowest(m["Scenario PnL vector"], _k99))
    # plain dispersion of the scenario P&L (per-observation sigma, same units as the returns): the
    # non-tail risk number that pairs with VaR/ES and feeds the diversification read below.
    m["Scenario PnL vol"] = tt.array.std(m["Scenario PnL vector"])
    # ---- exceedance rate (ch-08's simple calibration diagnostic, per CELL so it drills) ------
    # share of scenario days beyond ±2 of the cell's own vol. There is no elementwise compare
    # or abs for array measures, but elementwise / IS supported, so the exact 0/1 indicator is
    # positive_values(v−t)/(v−t): 1 above the threshold, 0/negative = 0 below (NaN only if an
    # element equals the threshold EXACTLY — measure-zero on continuous P&L).
    # NB negative_values/positive_values are LENGTH-PRESERVING (zero-fill, not filter) — a
    # len()-based count reads n and is wrong. Normal tails ≈ 4.6%; fatter reads higher.
    # Degenerate (blank) on length-1 Hypo sets (sample vol undefined).
    _v2 = 2.0 * m["Scenario PnL vol"]
    _up = m["Scenario PnL vector"] - _v2
    _dn = m["Scenario PnL vector"] + _v2
    m["Exceedance rate 2s"] = (
        (tt.array.sum(tt.array.positive_values(_up) / _up)
         + tt.array.sum(tt.array.negative_values(_dn) / _dn))
        / m["Scenario n"])
    m.fmt("Exceedance rate 2s", "DOUBLE[0.00%]")
    for k in ("Scenario VaR 95", "Scenario VaR 97.5", "Scenario ES 97.5",
              "Scenario ES 99", "Scenario PnL vol"):
        m.fmt(k, "DOUBLE[0.00%]")

    # (`Scenario dates (epoch)`, the index->date dual, is defined up with the P&L vector.)

    # date of the WORST scenario (argmin of the P&L vector), computed IN THE CUBE: the index of the
    # minimum read against the date dual. Lets the API report the worst-loss date without any
    # client/numpy argmin — the cube owns it, like Scenario worst loss / VaR 99.
    _worst_idx = tt.array.quantile_index(m["Scenario PnL vector"], 0.0, interpolation="lower")
    m["Scenario worst date (epoch)"] = m["Scenario dates (epoch)"][_worst_idx]

    # the P&L vector SORTED ascending — the empirical loss distribution, computed IN THE CUBE
    # (tt.array.sort). Lets a distribution chart show the full shape (sorted P&L vs percentile)
    # without any client-side binning/counting; the array primitives can't COUNT-per-bin anyway.
    m["Scenario PnL sorted"] = tt.array.sort(m["Scenario PnL vector"])
    _mark("measures.tail_and_dual")

    # ---- synthetic ScenarioDay dimension: UNPACK the P&L/date vectors into one row per array
    # element WITHOUT exploding the facts — the vector/array stays intact in the cube. A parameter
    # hierarchy of day-indices 0..N-1; its auto index measure picks the element at the CURRENT
    # member, so the same vector-indexing idiom used above (Scenario PnL vector[tail_idx]) projects
    # each day. Put ScenarioDay on a query axis -> the vector becomes a tabular per-day series (a
    # real pivot query: levels=[ScenarioDay] = the book path, +[Sector] = the sector breakout);
    # leave it OFF and every existing vector/scalar measure is byte-for-byte unchanged. N spans the
    # longest set (HistFull); shorter sets index past their length -> null (hide_empty drops them).
    # The calendar DATE rides along as the "Scenario date at day" measure, not a member — event
    # windows span different dates per set, so they can't be shared global members.
    #
    # SUPERSEDED FOR NEW WORK (2026-08-15): use the `Day` level and `PnL at day` defined below —
    # same numbers to the last bit, measured 10x faster (2.5 s vs 25 s on the largest book's full
    # history, 0.12 s vs ~25-50 s on an event set) with ~1 G of heap instead of ~15 G, and the
    # `x Sector` drill WORKS there where this one raises a BadArgumentException. This block stays
    # because the notebook and `author_chart_views.py` address it by name.
    N_days = int(scn_axis["DateVec"].map(len).max())   # longest set (HistFull) sizes the dimension
    cube.create_parameter_hierarchy_from_members(
        "ScenarioDay", list(range(N_days)), index_measure_name="ScenarioDay index")
    _mark("param_hierarchy.ScenarioDay")
    _day = m["ScenarioDay index"]
    # Scenario n (this set's vector length) is defined up with the tail measures above.
    # CLAMP the index in-bounds before reading the vector: Atoti errors the WHOLE query on an
    # out-of-range index (not a per-cell null), and shorter sets (COVID=82) are queried under the
    # full-size dimension (HistFull=2203). So index at a safe position (0 past the end) and NULL the
    # value beyond the set's own length — NON EMPTY / hide_empty then drop the surplus members.
    _safe = tt.where(_day < m["Scenario n"], _day, 0)
    _in = _day < m["Scenario n"]                       # this member is a real day of THIS set
    m["Scenario PnL at day"]          = tt.where(_in, m["Scenario PnL vector"][_safe], None)
    m["Scenario date at day (epoch)"] = tt.where(_in, m["Scenario dates (epoch)"][_safe], None)
    # chart-ready markers, ALSO gated by ScenarioDay so a per-day query is null past the set's length
    # (else these index-INDEPENDENT scalars stay non-null for every member and NON EMPTY can't trim
    # the query to its real days). VaR/worst are lifted to BOOK level (tt.total) so the rule/point are
    # the book's, identical whether or not Sector is on the axis; signs are baked for the chart (the
    # loss threshold and worst P&L are negative). Used by the COVID view's two graphs.
    _book_var  = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    _book_loss = tt.total(m["Scenario worst loss"], h["Security"], h["FactorDim"], h["PositionRank"])
    m["Scenario VaR line at day"]           = tt.where(_in, -_book_var, None)
    m["Scenario worst pnl at day"]          = tt.where(_in, -_book_loss, None)
    m["Scenario worst date at day (epoch)"] = tt.where(_in, m["Scenario worst date (epoch)"], None)
    for _mn in ("Scenario PnL at day", "Scenario VaR line at day", "Scenario worst pnl at day"):
        m.fmt(_mn, "DOUBLE[0.00%]")
    _mark("measures.scenario_day")

    # ---- THE FAST PER-DAY PATH: days as FACTS, not array indices (2026-08-15) -----------------
    # Same numbers as `Scenario PnL at day`, reached by ordinary additive aggregation over the
    # ScenarioDays table (build_scenario_days) instead of 2,618 vector-index evaluations. USE THIS
    # for any per-day drill; the ScenarioDay parameter hierarchy above is kept for backward
    # compatibility only (the notebook idiom) and is ~40x slower.
    #
    #   book path      cube.query(m["PnL at day"], levels=[l["Day"], l["DayDate"]],
    #                             filter=(l["Date"] == d) & (l["Book"] == b) & (l["DaySet"] == "HistFull"))
    #   day x sector   ... levels=[l["Day"], l["Sector"]]  (the shape the old path could not do at all)
    #
    # Take the calendar date off the `DayDate` LEVEL (1:1 with Day, so it adds no rows) rather than
    # the `Date at day (epoch)` measure: a level is read off the axis, a measure is aggregated per
    # member -- measured, the epoch measure cost about as much again as the P&L itself.
    #
    # `DaySet` is the day table's OWN set key -- slice it, not `ScenarioSet`, for these two measures
    # (a `ScenarioSet` slice is harmless but selects nothing here; the two hierarchies carry the same
    # member names for the 7 real sets). Members of Day are per-set real days, so a DaySet slice
    # already trims the axis to that set's length -- no in-range gate, no clamp, nothing to null out.
    assert {"DaySet", "Day", "DayDate"} <= {n for _, n in h}, sorted(n for _, n in h)
    # THE LIFT IS THE WHOLE OPTIMIZATION. Without it (measured, attempt 1) this measure costs
    # 30.8 s on Vanguard/HistFull and +20 G of heap -- WORSE than the parameter hierarchy it
    # replaces -- because the day table is joined to the FACT table, so every one of the 2,618 day
    # members re-derives `Net exposure` from the 84k (Position x Factor) leaves in its own cell:
    # cost ~ days x book size (COVID's 82 days measured 0.83 s, the small book 1.47 s, both on the
    # same code). `tt.total(..., h["DaySet"], h["Day"])` lifts the day hierarchies to their top
    # INSIDE the exposure term, so the book's 23 factor exposures are computed ONCE at the
    # day-independent context and every day member reuses them; only the shock scalar varies.
    _exp_ex_day = tt.total(m["Net exposure"], h["DaySet"], h["Day"], h["DayDate"])
    m["PnL at day"] = tt.agg.sum(
        _exp_ex_day * tt.agg.single_value(t_days["ShockAtDay"]),
        scope=tt.OriginScope({l["Factor"]}))
    m.fmt("PnL at day", "DOUBLE[0.00%]")   # deferred: the batched publish owns formatters
    # The date dual, epoch days like `Scenario dates (epoch)`. MAX, not SUM: the epoch is the same
    # on every factor row of a (DaySet, Day), so max reads it once instead of fanning out 23x (the
    # joined-column fan-out trap the SpecificVar note above flags).
    m["Date at day (epoch)"] = tt.agg.max(t_days["DayEpoch"])
    # Chart-ready markers for the Day path (the twins of `Scenario VaR line at day` & co. above,
    # 2026-08-15): book-level VaR / worst-loss / worst-date, LIFTED over the day hierarchies too,
    # so a `rows=[Day, DayDate]` chart query reads each once (day-independent context) instead of
    # re-deriving the book vector per day member -- the same lift that makes `PnL at day` fast.
    # No in-range gate needed: Day members are per-set real days once DaySet is sliced. Signs are
    # baked for the chart (loss threshold and worst P&L negative), same as the legacy markers.
    _day_h = (h["DaySet"], h["Day"], h["DayDate"])
    m["VaR line at day"] = -tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"],
                                     h["PositionRank"], *_day_h)
    m["Worst pnl at day"] = -tt.total(m["Scenario worst loss"], h["Security"], h["FactorDim"],
                                      h["PositionRank"], *_day_h)
    m["Worst date at day (epoch)"] = tt.total(m["Scenario worst date (epoch)"], h["Security"],
                                              h["FactorDim"], h["PositionRank"], *_day_h)
    for _mn in ("VaR line at day", "Worst pnl at day"):
        m.fmt(_mn, "DOUBLE[0.00%]")

    # ---- diagonal specific block (additive, scenario-independent) -----------------------------
    # OriginScope + single_value, NOT a columnar SUM: see the fan-out note at the top of
    # build_cube. Cheap regardless — one term per (Date, Position) member in context.
    wgt = tt.agg.single_value(t_pos["Weight"])
    m["Specific variance"] = tt.agg.sum(
        wgt * wgt * tt.agg.single_value(t_sv["SpecificVar"]),
        scope=tt.OriginScope({l["Date"], l["Position"]}))
    m["Specific vol"] = tt.math.sqrt(m["Specific variance"])
    # gross/net book weight from the JOINED Positions Weight (branch-sensitive like everything
    # else; one term per (Date, Position) like Specific variance). In-cube identity: Net weight
    # == Net exposure at Factor=Market (the unit Market loading makes x_Market = Σ weights).
    m["Net weight"] = tt.agg.sum(wgt, scope=tt.OriginScope({l["Date"], l["Position"]}))
    m["Gross weight"] = tt.agg.sum(tt.math.abs(wgt),
                                   scope=tt.OriginScope({l["Date"], l["Position"]}))
    for _mn in ("Net weight", "Gross weight"):
        m.fmt(_mn, "DOUBLE[0.000]")
    # ---- model vol: THE reference risk number (2026-07-03), sigma = sqrt(x'Fx + w'dw) ---------
    # Factor half = std of the scenario P&L vector (atoti 'sample' mode == np.cov ddof=1), so
    # sliced to HistFull this IS the model sigma on the same full-history covariance the API uses
    # (_risk_from_weights / _euler_contributions tie out to float precision). On Evt:* it reads
    # as the window (regime) vol; on length-1 Hypo:* the sample std is degenerate — blank.
    # Computed per cell (each slice's own vector + own specific block), so it drills by
    # sector/name/factor like the VaR measures.
    m["Model vol"] = tt.math.sqrt(m["Scenario PnL vol"] ** 2 + m["Specific variance"])
    m.fmt("Model vol", "DOUBLE[0.00%]")
    _mark("measures.specific_and_modelvol")

    # ---- the PIT mirror: the SAME engine driven by the PITSet switch --------------------------
    # Exactly the three measures the honest-vol path consumes (`_pred_book_vols` in risk_api:
    # per month d0 it reads book sigma, its specific half, and the per-factor P&L vol at
    # PITSet=PIT:d0). Nothing else is mirrored -- the PIT sets are plumbing for as-of risk, not a
    # browsable scenario family, and each mirrored measure is another vector expression to
    # evaluate. `Specific vol`/`Specific variance` need no mirror: they are scenario-independent
    # and read correctly under a PITSet slice like any other.
    if t_pit is not None:
        # (`PIT Scenario PnL vector` is defined up with the other array-typed measures.)
        m["PIT Scenario PnL vol"] = tt.array.std(m["PIT Scenario PnL vector"])
        m["PIT Model vol"] = tt.math.sqrt(m["PIT Scenario PnL vol"] ** 2 + m["Specific variance"])
        for _mn in ("PIT Scenario PnL vol", "PIT Model vol"):
            m.fmt(_mn, "DOUBLE[0.00%]")
    _mark("measures.pit_mirror")
    # approximate total tail: factor scenario VaR with an independent idiosyncratic tail (z=2.326)
    m["Total VaR 99"] = tt.math.sqrt(m["Scenario VaR 99"] * m["Scenario VaR 99"]
                                     + (2.326 * m["Specific vol"]) ** 2)
    m.fmt("Specific vol", "DOUBLE[0.00%]")
    m.fmt("Total VaR 99", "DOUBLE[0.00%]")
    # Expected-Shortfall analogue of Total VaR: factor-ES combined in quadrature with the
    # idiosyncratic tail. For a normal tail the ES97.5 multiplier is phi(z)/(1-a) = 2.338 (~ the
    # 2.326 used for 99% VaR -- ES97.5 == VaR99 under normality), applied to the specific vol.
    m["Total ES 97.5"] = tt.math.sqrt(m["Scenario ES 97.5"] * m["Scenario ES 97.5"]
                                      + (2.338 * m["Specific vol"]) ** 2)
    m.fmt("Total ES 97.5", "DOUBLE[0.00%]")

    # ---- Price family: historical simulation on RAW STOCK RETURNS, no factor model ------------
    # (docs/price-var-plan.md.) Second copy of the Scenario vector engine, mirrored measure for
    # measure and Euler convention for Euler convention: `Price PnL vector` (defined above, per
    # POSITION via OriginScope) plays the role of `Scenario PnL vector` throughout. PriceSet is
    # the switch hierarchy (HistFull + every Evt:* window — the SAME calendar as ScenarioSet by
    # construction, via build_price_returns/build_price_axis; Hypo:* sets are sigma-shocks on
    # factors and have no Price mirror). MEANINGFUL in by-NAME/Sector/Issuer/book views; by
    # FACTOR it repeats (Price has no factor structure at all) — same fan-out caveat
    # Specific variance / Specific PnL already carry. `has_price` (stock_returns.parquet present,
    # v2-only) degrades cleanly: v1 data / a pre-Price-VaR build gets no Price measures.
    if has_price:
        book_price_vec = tt.total(m["Price PnL vector"], h["Security"], h["FactorDim"], h["PositionRank"])
        price_tail_idx = tt.array.quantile_index(book_price_vec, 0.01, interpolation="lower")

        m["Price mean PnL"]   = tt.array.mean(m["Price PnL vector"])
        m["Price VaR 95"]     = -tt.array.quantile(m["Price PnL vector"], 0.05)
        m["Price VaR 97.5"]   = -tt.array.quantile(m["Price PnL vector"], 0.025)
        m["Price VaR 99"]     = -tt.array.quantile(m["Price PnL vector"], 0.01)
        m["Price worst loss"] = -tt.array.min(m["Price PnL vector"])
        _price_n = tt.array.len(m["Price PnL vector"])                 # this set's vector length
        _pk975 = tt.math.ceil(0.025 * _price_n)                        # tail size for 97.5% ES
        _pk99  = tt.math.ceil(0.01  * _price_n)                        # tail size for 99% ES
        m["Price ES 97.5"] = -tt.array.mean(tt.array.n_lowest(m["Price PnL vector"], _pk975))
        m["Price ES 99"]   = -tt.array.mean(tt.array.n_lowest(m["Price PnL vector"], _pk99))
        m["Price PnL vol"] = tt.array.std(m["Price PnL vector"])
        for _mn in ("Price mean PnL", "Price VaR 95", "Price VaR 97.5", "Price VaR 99",
                    "Price worst loss", "Price ES 97.5", "Price ES 99", "Price PnL vol"):
            m.fmt(_mn, "DOUBLE[0.00%]")

        # --- Marginal Price VaR 99: the SAME tail-day Euler as Marginal Scenario VaR 99 — this
        #     cell's OWN P&L on the BOOK's 1% tail scenario. ADDITIVE: Σ_member = Price VaR 99.
        m["Marginal Price VaR 99"] = -m["Price PnL vector"][price_tail_idx]
        m["% of Price VaR 99"] = (m["Marginal Price VaR 99"]
            / tt.total(m["Marginal Price VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))
        _pk975_book = tt.math.ceil(0.025 * tt.array.len(book_price_vec))
        _price_book_tail_idx = tt.array.n_lowest_indices(book_price_vec, _pk975_book)
        m["Marginal Price ES 97.5"] = -tt.array.mean(m["Price PnL vector"][_price_book_tail_idx])
        for _mn in ("Marginal Price VaR 99", "Marginal Price ES 97.5"):
            m.fmt(_mn, "DOUBLE[0.00%]")
        m.fmt("% of Price VaR 99", "DOUBLE[0.0%]")

        # --- Incremental Price VaR 99: book minus book-without-member, EXACTLY like Incremental
        #     Scenario VaR 99 — diversification-aware, NOT additive, no "% of".
        price_var_book = -book_price_vec[price_tail_idx]
        price_pnl_ex = book_price_vec - m["Price PnL vector"]
        price_tail_idx_ex = tt.array.quantile_index(price_pnl_ex, 0.01, interpolation="lower")
        price_var_ex = -price_pnl_ex[price_tail_idx_ex]
        m["Incremental Price VaR 99"] = price_var_book - price_var_ex
        m.fmt("Incremental Price VaR 99", "DOUBLE[0.00%]")

        # --- Price coverage at tail: the book's weight share that was ACTUALLY priced on its own
        #     Price VaR tail day (never the model's tail day — this is a model-free lens). Reads
        #     1.0 for a fully-priced book on that day; the /var_bridge disclosure for the rest.
        book_coverage_vec = tt.total(m["Price coverage vector"], h["Security"], h["FactorDim"], h["PositionRank"])
        m["Price coverage at tail"] = book_coverage_vec[price_tail_idx]
        m.fmt("Price coverage at tail", "DOUBLE[0.0%]")
    _mark("measures.price_family")

    # ---- PnL attribution measures (Step 15; only when the 7th frame was built) ----------------
    # All three are ADDITIVE scalars (same class as Net exposure — no ragged vectors), reading the
    # forward-month convention set at load: the value at Date d0 is the PnL over (d0, d1].
    #   Factor contribution — a plain columnar SUM of the precomputed leaf product
    #                         w_i·L_ik·f_k(fwd month); Σ over names = x_k·f_k, so Factor→Name
    #                         drills foot exactly.
    #   Specific PnL        — w_i·ε_i(fwd month) per (Date, Position) via OriginScope+single_value
    #                         (a joined table: a plain SUM would fan out per factor leaf, the same
    #                         trap the SpecificVar note above flags). By FACTOR it fans out —
    #                         specific risk has no factor — use it in by-name/book views.
    #   Realized PnL        — the identity Factor contribution + Specific PnL.
    if t_sr is not None:
        # KNOWN LIMITATION (Phase 2, multi-manager): book-independent, see the `w` note near the
        # top of build_cube -- reads ONE arbitrary book's weight for a name held by >1 book.
        m["Factor contribution"] = tt.agg.sum(t_exp["FactorPnL"])
        m["Specific PnL"] = tt.agg.sum(
            tt.agg.single_value(t_sr["SpecPnL"]),
            scope=tt.OriginScope({l["Date"], l["Position"]}))
        m["Realized PnL"] = m["Factor contribution"] + m["Specific PnL"]
        for _mn in ("Factor contribution", "Specific PnL", "Realized PnL"):
            m.fmt(_mn, "DOUBLE[0.00%]")
    _mark("measures.attribution")

    # ---- Level-2 risk decomposition: additive contributions to Scenario VaR 99 ---------------
    # The factor-VaR is the book loss on the tail scenario t* (the 1%-quantile day of the BOOK
    # P&L vector). A member's contribution is its OWN P&L on that SAME book scenario, so the
    # contributions are additive: Σ_member Component = book P&L at t* = Scenario VaR 99.
    #   book_pnl_vec  -> the full-book P&L vector regardless of the current Factor/Security cell
    #                    (tt.total lifts those hierarchies to their top; Date/Book/ScenarioSet
    #                    stay on the current slice, so the tail is the sliced book's tail).
    #   tail_idx      -> index of that book vector's 1% quantile (the VaR scenario).
    book_pnl_vec = tt.total(m["Scenario PnL vector"], h["Security"], h["FactorDim"], h["PositionRank"])
    tail_idx = tt.array.quantile_index(book_pnl_vec, 0.01, interpolation="lower")
    # --- contribution to the FACTOR VaR (Scenario VaR 99): the current cell's P&L at the book's
    #     tail scenario. ADDITIVE: Σ_member = Scenario VaR 99. ("marginal" in the Flex Agg sense.)
    m["Marginal Scenario VaR 99"] = -m["Scenario PnL vector"][tail_idx]
    m["VaR sensitivity"] = m["Marginal Scenario VaR 99"] / m["Net exposure"]   # per-unit ∂VaR/∂exp
    # share of factor VaR: divide by the SUM of the marginals (NOT the interpolated quantile) so
    # it sums to EXACTLY 100%.
    m["% of Scenario VaR 99"] = (m["Marginal Scenario VaR 99"]
        / tt.total(m["Marginal Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # --- contribution to the FACTOR ES 97.5: a member's MEAN P&L over the BOOK's worst-k tail
    #     scenarios (k = ceil(0.025 * n) lowest days of the BOOK P&L vector). Additive like the
    #     VaR marginal (Σ_member = Scenario ES 97.5) but averaged over the whole tail SET rather
    #     than read off the single 1% day -- ES is coherent, so this split is better-behaved.
    #     book_tail_idx = the k lowest INDICES of the book vector; indexing each member's own P&L
    #     vector by that index-array picks its P&L on exactly those book-tail days.
    _k975_book = tt.math.ceil(0.025 * tt.array.len(book_pnl_vec))
    _book_tail_idx = tt.array.n_lowest_indices(book_pnl_vec, _k975_book)
    m["Marginal Scenario ES 97.5"] = -tt.array.mean(m["Scenario PnL vector"][_book_tail_idx])
    m["% of Scenario ES 97.5"] = (m["Marginal Scenario ES 97.5"]
        / tt.total(m["Marginal Scenario ES 97.5"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # --- contribution to TOTAL VaR 99 (factor + specific, combined in quadrature). Euler split of
    #     Total=√(F²+S²): each factor-marginal is scaled by F/Total, and the idiosyncratic block
    #     adds z²·(w²σ²)/Total PER NAME. Σ_member = Total VaR 99.
    #     NB: meaningful only in by-NAME views (Issuer/Sector/Country/Position). By FACTOR the
    #     specific part fans out (specific risk has no factor) -> use Marginal Scenario VaR 99 there.
    F_book = tt.total(m["Scenario VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])     # book factor VaR
    T_book = tt.total(m["Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])        # book total VaR
    m["Marginal Total VaR 99"] = (m["Marginal Scenario VaR 99"] * F_book / T_book
                                  + (2.326 ** 2) * m["Specific variance"] / T_book)
    m["% of Total VaR 99"] = (m["Marginal Total VaR 99"]
        / tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))

    # ---- concentration: Herfindahl-Hirschman index of risk shares. HHI = Σ_name share², where
    #      share is a NAME's fraction of book Total VaR (its Marginal Total VaR / the book total).
    #      Summed at the POSITION grain via an OriginScope (factors lifted, one term per name),
    #      like Specific variance. Reads 1/N for an evenly-diversified book up to 1.0 for a single
    #      name; the standard single-number concentration gauge a desk watches against a limit.
    _name_share = (m["Marginal Total VaR 99"]
                   / tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"]))
    m["Risk HHI"] = tt.agg.sum(_name_share * _name_share, scope=tt.OriginScope({l["Position"]}))
    m.fmt("Risk HHI", "DOUBLE[0.000]")
    _mark("measures.decomposition")

    # ---- Top-5 risk share: the /limits concentration metric, cube-native via tt.rank ----------
    # tt.rank ranks SIBLINGS, and in the multilevel Security hierarchy a position's siblings are
    # the positions under the same Issuer (~1:1 here — every rank would read 1). The FLAT
    # PositionRank hierarchy (created with the other hierarchies above) gives the global
    # ranking; every book-level tt.total in this file lifts it, so measures evaluated inside a
    # PositionRank context still see the true book totals.
    _tot_r = tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    _rank_r = tt.rank(m["Marginal Total VaR 99"], h["PositionRank"], ascending=False)
    m["Top-5 risk share"] = tt.agg.sum(
        tt.where(_rank_r <= 5, m["Marginal Total VaR 99"] / _tot_r, 0.0),
        scope=tt.OriginScope({l["PositionR"]}))
    m.fmt("Top-5 risk share", "DOUBLE[0.0%]")
    _mark("measures.top5_rank")

    # --- INCREMENTAL VaR (Flex Agg sense): REMOVE the current member, recompute the BOOK VaR on
    #     the reduced portfolio, and subtract it from the reference book VaR. Unlike the marginals
    #     this is NOT additive, so it deliberately has no "% of" share. It answers "how much VaR
    #     does removing this member RELEASE", the diversification-aware view a manager uses to
    #     decide what to cut.
    #     This comment used to add "(VaR is sub-additive) — Σ_member Incremental ≤ book VaR".
    #     That is wrong twice over and test_risk_measures had been failing on it: a 99% quantile
    #     is the textbook measure that is NOT sub-additive, and the removal re-reads the reduced
    #     book at ITS OWN tail day (see tail_idx_ex below), so each member is credited for
    #     shifting the tail as well as for its own risk. Measured 2026-08-21 on Soros/HistFull by
    #     Issuer: Σ Incremental Scenario VaR 0.0428 vs book 0.0353. The sum-under-book property
    #     belongs to `Incremental Model vol` (a standard deviation), where it holds and is pinned.
    #     REFERENCE = the additive book VaR (Σ of the marginals = the tail-scenario READ-OFF), NOT
    #     the interpolated quantile F_book/T_book — so the col-TOTAL row reconciles with the Marginal
    #     column total to the last digit (at the grand level the removed book is empty -> VaR_ex=0,
    #     leaving Incremental == reference == Marginal total). Mixing the two quantile conventions is
    #     exactly what made the totals disagree by ~0.001.
    #       book_var -> reference book factor-VaR = book P&L at the book's own tail scenario.
    #       pnl_ex   -> the book P&L vector with THIS cell's own P&L removed (elementwise array sub).
    #       VaR_ex   -> book factor-VaR recomputed on the reduced vector at ITS OWN tail (removal can
    #                   shift the tail day) — same lower-index read-off convention as the book.
    book_var = -book_pnl_vec[tail_idx]                                   # == tt.total(Marginal Scenario VaR 99)
    pnl_ex = book_pnl_vec - m["Scenario PnL vector"]
    tail_idx_ex = tt.array.quantile_index(pnl_ex, 0.01, interpolation="lower")
    VaR_ex = -pnl_ex[tail_idx_ex]
    m["Incremental Scenario VaR 99"] = book_var - VaR_ex
    # total-VaR analog: strip the member's specific variance too, recombine in quadrature, subtract.
    # Reference = tt.total(Marginal Total VaR 99) (the additive book total) for the same reconciliation.
    book_total = tt.total(m["Marginal Total VaR 99"], h["Security"], h["FactorDim"], h["PositionRank"])
    S_ex_var = tt.total(m["Specific variance"], h["Security"], h["FactorDim"], h["PositionRank"]) - m["Specific variance"]
    Total_ex = tt.math.sqrt(VaR_ex * VaR_ex + (2.326 ** 2) * S_ex_var)
    m["Incremental Total VaR 99"] = book_total - Total_ex
    _mark("measures.incremental")

    # ---- Model-vol decomposition: Euler marginal (== CTR) + incremental -----------------------
    # Marginal Model vol: the member's EULER contribution to book sigma — cov(member P&L vector,
    # book P&L vector) plus the member's own specific variance, over book sigma. The covariance
    # comes from the polarization identity cov(a,b) = (var(a+b) − var(a) − var(b))/2 on the SAME
    # sample std Model vol uses, so Σ_member == book Model vol EXACTLY (Euler) and the per-NAME
    # values equal the ch-09 CTR = w·(Σw)/σ that /contributions computes in numpy.
    # Like Marginal Total VaR: meaningful in by-NAME views; by FACTOR the specific block fans out.
    _sigma_book = tt.total(m["Model vol"], h["Security"], h["FactorDim"], h["PositionRank"])
    _cov_book = (tt.array.std(book_pnl_vec + m["Scenario PnL vector"]) ** 2
                 - m["Scenario PnL vol"] ** 2
                 - tt.array.std(book_pnl_vec) ** 2) / 2
    m["Marginal Model vol"] = (_cov_book + m["Specific variance"]) / _sigma_book
    m["% of Model vol"] = (m["Marginal Model vol"]
        / tt.total(m["Marginal Model vol"], h["Security"], h["FactorDim"], h["PositionRank"]))
    # The clean FACTOR-side contribution (no specific block, VARIANCE units): cov(member, book).
    # By Factor: per factor this IS the ch-09 CTV = x_k(Fx)_k (cross-terms 50/50, negative =
    # hedge) and Σ_factor = the factor variance x'Fx. /contributions serves it.
    m["Factor variance contribution"] = _cov_book
    _mark("measures.euler_modelvol")

    # ---- Tier-1 cube migrations (docs/cube-measure-opportunities.md) -----------------------
    # Factor return vol: std of the RAW factor-return vector (the ShockVec itself, exposure-
    # free) — per Factor on HistFull this is vol_k = std(f_k), the same wide.std() estimator
    # /stress and /reverse_stress used from pandas; on Evt:* sets it reads the window vol.
    # In-cube identity: Scenario PnL vol == |Net exposure| * Factor return vol at any Factor
    # member (std(x·f) = |x|·std(f)).
    _shock_vec = tt.agg.single_value(t_scn["ShockVec"])
    m["Factor return vol"] = tt.array.std(_shock_vec)
    m.fmt("Factor return vol", "DOUBLE[0.00%]")
    # Vol ex factor: book sigma with the current cell's FACTOR P&L removed but the FULL specific
    # block kept — by FACTOR this is the hedge table's vol-after-neutralizing-k (zeroing x_k
    # cannot touch specific risk; NB Incremental Model vol strips the cell's specific, which is
    # right for NAMES and wrong here — the fan-out trap, handled explicitly).
    _svar_book = tt.total(m["Specific variance"], h["Security"], h["FactorDim"], h["PositionRank"])
    m["Vol ex factor"] = tt.math.sqrt(tt.array.std(pnl_ex) ** 2 + _svar_book)
    m.fmt("Vol ex factor", "DOUBLE[0.00%]")
    # Min-variance hedge ratio (appendix D6), in EXPOSURE units: h* = −(Fx)_k/F_kk. Via the
    # polarization cov: cov(book, v_k) = x_k·(Fx)_k and var(f_k) = F_kk, so
    # h* = −cov/(x_k·vol_k²) — algebraically x_k cancels out of (Fx)_k, so a near-zero exposure
    # still reads the true ratio (0/0 -> blank only at exactly zero).
    m["Min-variance hedge ratio"] = (-_cov_book
        / (m["Net exposure"] * m["Factor return vol"] ** 2))
    m.fmt("Min-variance hedge ratio", "DOUBLE[0.000]")
    # Vol at min-variance hedge: book sigma after ADDING h* units of the pure factor-k return
    # stream (specific block untouched) — the D6 single-instrument hedge priced per slice.
    _hedged_vec = book_pnl_vec + m["Min-variance hedge ratio"] * _shock_vec
    m["Vol at min-variance hedge"] = tt.math.sqrt(tt.array.std(_hedged_vec) ** 2 + _svar_book)
    m.fmt("Vol at min-variance hedge", "DOUBLE[0.00%]")
    _mark("measures.tier1_hedge")

    # ---- Stressed model vol: the correlation stress as a PARAMETERIZED measure ----------------
    # x'F'x under vols x m and correlations blended b toward 1 has a closed form needing no
    # matrix algebra:  m^2 * [ (1-b) * x'Fx  +  b * (SUM_k x_k*sigma_k)^2 ]   (D J D expansion),
    # and the specific block scales m^2. Same math as _stressed_cov, per CELL — so "this
    # sector's vol when correlations go to 1" is a drill, which the API version can't do.
    # Parameters live on the CorrStress simulation (Base: mult 1, blend 0 -> equals Model vol).
    cube.create_parameter_simulation(
        "CorrStress", measures={"Vol mult": 1.0, "Rho blend": 0.0})
    _mark("param_sim.CorrStress")
    _sxs = tt.agg.sum(m["Net exposure"] * m["Factor return vol"],
                      scope=tt.OriginScope({l["Factor"]}))          # SUM_k x_k * sigma_k (signed)
    m["Stressed model vol"] = m["Vol mult"] * tt.math.sqrt(
        (1.0 - m["Rho blend"]) * m["Scenario PnL vol"] ** 2
        + m["Rho blend"] * _sxs ** 2
        + m["Specific variance"])
    m.fmt("Stressed model vol", "DOUBLE[0.00%]")

    # ---- Tier-2 prototype: custom stress as a PARAMETER SIMULATION ----------------------------
    # (docs/cube-measure-opportunities.md #3.) A per-Factor "Shock sigma" parameter, default 0 on
    # the Base scenario. /stress appends a TRANSIENT scenario per request (uuid rows into the
    # "StressShock" table), reads `Custom stress PnL` on that branch, and drops the rows. What
    # the API math cannot do and this gets free: the shock P&L drills by name/sector (each leaf
    # is w·L_k·sigma_k·vol_k, footing exactly), and composes with any cube filter.
    # NB Factor return vol is ScenarioSet-dependent — always read this sliced to HistFull.
    _mark("measures.stressed_vol")
    cube.create_parameter_simulation(
        "StressShock", measures={"Shock sigma": 0.0}, levels=[l["Factor"]])
    _mark("param_sim.StressShock")
    m["Custom stress PnL"] = tt.agg.sum(
        m["Net exposure"] * m["Shock sigma"] * m["Factor return vol"],
        scope=tt.OriginScope({l["Factor"]}))
    m.fmt("Custom stress PnL", "DOUBLE[0.00%]")
    # Incremental Model vol: REMOVE the member, recompute sigma on the remainder (its factor
    # vector minus this cell's, its specific variance minus this cell's), subtract from the book
    # sigma. NOT additive (vol is sub-additive) — it answers "how much vol does removing this
    # member release". At the grand level the remainder is empty -> vol_ex = 0, so the total
    # reconciles with the Marginal column (both read the book sigma).
    _vol_ex = tt.math.sqrt(tt.array.std(pnl_ex) ** 2 + S_ex_var)
    m["Incremental Model vol"] = _sigma_book - _vol_ex
    for _mn in ("Marginal Model vol", "Incremental Model vol"):
        m.fmt(_mn, "DOUBLE[0.00%]")

    for _mn in ("Marginal Scenario VaR 99", "Marginal Total VaR 99",
                "Incremental Scenario VaR 99", "Incremental Total VaR 99",
                "Marginal Scenario ES 97.5"):
        m.fmt(_mn, "DOUBLE[0.00%]")
    m.fmt("VaR sensitivity", "DOUBLE[0.0000]")
    for _mn in ("% of Scenario VaR 99", "% of Total VaR 99", "% of Scenario ES 97.5"):
        m.fmt(_mn, "DOUBLE[0.0%]")
    # ---- dollars (2026-08-22): the 13F market value joins the cube ---------------------------
    # Market value = $ held in the cell (one term per (Date, Position), the Specific-variance
    # idiom — never a columnar sum across the factor fan-out). Book MV lifts it to the sliced
    # book over every non-book hierarchy (incl. the Day path, so PnL at day $ works), and each
    # weight-unit measure gets a "$" twin = measure × Book MV. A what-if branch moves Weight,
    # not MV, so a hypothetical's $ figures are priced on the base book size — disclosed.
    _mv = tt.agg.single_value(t_pos["MV"])
    m["Market value"] = tt.agg.sum(_mv, scope=tt.OriginScope({l["Date"], l["Position"]}))
    m["Book MV"] = tt.total(m["Market value"], h["Security"], h["FactorDim"], h["PositionRank"],
                            *_day_h)
    for _mn in ("Market value", "Book MV"):
        m.fmt(_mn, "DOUBLE[#,##0]")
    for _mn in DOLLAR_MEASURES:
        if _mn not in m:              # e.g. the Price family, absent when stock_returns is absent
            continue
        m[f"{_mn} $"] = m[_mn] * m["Book MV"]
        m.fmt(f"{_mn} $", "DOUBLE[#,##0]")
    _mark("measures.dollars")
    _mark("measures.define")
    # ONE publish for every measure above, then the formatters (see _DeferredMeasures).
    m.flush()
    _mark("measures.flush")

    # ---- DATA LAST: the whole model is now defined; fill the three big tables (see the seeding
    # note by the loads). The seed rows are re-loaded inside the full frames and upsert on the
    # table keys, so the result is exactly the frames, once.
    _bulk = [(t_exp, exposures), (t_pos, positions_cube), (t_sv, specific)]
    if t_sr is not None:
        _bulk.append((t_sr, spec_pnl))
    if t_price is not None:
        _bulk.append((t_price, price_ret))
    _bulk_load(_bulk)
    _mark("load.bulk")

    print(f"cube built: {len(exposures):,} leaf rows, {len(style)} style factors, "
          f"{scenarios['ScenarioSet'].nunique()} scenario sets, "
          f"{pit_scn['PITSet'].nunique()} PIT sets, {len(scn_days):,} scenario-day rows")
    return session, cube


if __name__ == "__main__":
    session, cube = build_cube(load_frames())
    h, l, m = cube.hierarchies, cube.levels, cube.measures
    last = sorted(cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index)[-1]
    last = pd.Timestamp(last).date()    # Date level stores LocalDate; a datetime filter fails to parse

    # exposures drill (additive)
    print(cube.query(m["Net exposure"], levels=[l["FactorGroup"], l["Factor"]]))

    # THE SWITCH: same measure, every scenario mode, side by side (as-of latest COB)
    print(cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Total VaR 99"],
                     levels=[l["ScenarioSet"]], filter=l["Date"] == last))

    # tail board: VaR ladder + Expected Shortfall + dispersion across every scenario set
    print(cube.query(m["Scenario VaR 95"], m["Scenario VaR 97.5"], m["Scenario VaR 99"],
                     m["Scenario ES 97.5"], m["Scenario ES 99"], m["Scenario PnL vol"],
                     m["Total ES 97.5"], m["Risk HHI"],
                     levels=[l["ScenarioSet"]], filter=l["Date"] == last))

    # factor ES contributions (additive: factors sum to book Scenario ES 97.5)
    print(cube.query(m["Marginal Scenario ES 97.5"], m["% of Scenario ES 97.5"],
                     levels=[l["Factor"]], filter=(l["Date"] == last) & (l["ScenarioSet"] == "HistFull")))

    # any scenario mode still drills the full hierarchy -- e.g. COVID replay by sector
    print(cube.query(m["Scenario worst loss"], levels=[l["Sector"]],
                     filter=(l["Date"] == last) & (l["ScenarioSet"] == "Evt:COVID2020")))

    session.wait()
