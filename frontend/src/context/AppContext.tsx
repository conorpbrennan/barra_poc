// The one IA change (docs/vite-ui-plan.md §9): manager / as-of date / scenario-set hoisted into a
// single global context every lens inherits, instead of per-panel selectors. Defaults: latest
// date, HistFull, Millennium. Persisted to the URL query so a view is shareable/bookmarkable.
import { createContext, useContext, useMemo, useState, useEffect, ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useMeta } from "../api/hooks";
import type { Manager } from "../api/types";

export interface AppCtx {
  manager: string;
  date: string;          // as-of date (latest by default)
  scenario: string;      // global ScenarioSet
  dates: string[];
  scenarioSets: string[];
  managers: Manager[];   // the entity picker's source of truth (multi-manager Phase 4) — from
                          // /meta.managers (never a hardcoded list), "N/A" filtered defensively
  ready: boolean;
  setManager: (m: string) => void;
  setDate: (d: string) => void;
  setScenario: (s: string) => void;
}

const Ctx = createContext<AppCtx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const { data: meta } = useMeta();
  const [params, setParams] = useSearchParams();

  // ?book= is still read for backward compatibility with old links; ?manager= takes priority.
  const [manager, setManager] = useState(params.get("manager") || params.get("book") || "Millennium");
  const [date, setDate] = useState(params.get("date") || "");
  const [scenario, setScenario] = useState(params.get("set") || "HistFull");

  const dates = meta?.dates ?? [];
  const scenarioSets = meta?.scenario_sets ?? [];
  // Defensive filter (not a fix): the live /dims endpoint has been observed reporting an extra
  // "N/A" Manager member alongside the real managers (pre-existing, cause not identified — see
  // scratchpad/phase3-notes.md item 5). /meta.managers is sourced from the positions frame
  // directly and is clean today, but a manager list is exactly the kind of thing that could pick
  // that up some other way later, so it's filtered here rather than trusted blindly. Memoized on
  // meta.managers's own reference (stable across re-renders until a refetch) so this doesn't
  // create a new array every render and re-trigger the effect below for no reason.
  const managers = useMemo(
    () => (meta?.managers ?? []).filter((m) => m.manager && m.manager !== "N/A"),
    [meta?.managers],
  );

  // once meta lands, default the date to the latest available (if not already URL-set)
  useEffect(() => {
    if (!date && dates.length) setDate(dates[dates.length - 1]);
  }, [dates, date]);

  // once managers land, if the current manager isn't one of them (a stale/invalid ?manager= URL
  // param, or the default above on data where that manager doesn't actually exist),
  // default to the first known manager — mirrors the date-defaulting effect above.
  useEffect(() => {
    if (managers.length && !managers.some((m) => m.manager === manager)) setManager(managers[0].manager);
  }, [managers, manager]);

  // reflect context into the URL (shareable), without stomping other params
  useEffect(() => {
    const next = new URLSearchParams(params);
    next.set("manager", manager);
    next.delete("book"); // normalise any legacy ?book= link to ?manager= on first render
    if (date) next.set("date", date);
    next.set("set", scenario);
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manager, date, scenario]);

  const value = useMemo<AppCtx>(
    () => ({
      manager, date, scenario, dates, scenarioSets, managers,
      ready: !!meta && !!date,
      setManager, setDate, setScenario,
    }),
    [manager, date, scenario, dates, scenarioSets, managers, meta],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp must be used within AppProvider");
  return v;
}
