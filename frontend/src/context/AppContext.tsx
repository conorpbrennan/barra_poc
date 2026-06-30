// The one IA change (docs/vite-ui-plan.md §9): book / as-of date / scenario-set hoisted into a
// single global context every lens inherits, instead of per-panel selectors. Defaults: latest
// date, HistFull, Soros. Persisted to the URL query so a view is shareable/bookmarkable.
import { createContext, useContext, useMemo, useState, useEffect, ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useMeta } from "../api/hooks";

export interface AppCtx {
  book: string;
  date: string;          // as-of date (latest by default)
  scenario: string;      // global ScenarioSet
  dates: string[];
  scenarioSets: string[];
  ready: boolean;
  setBook: (b: string) => void;
  setDate: (d: string) => void;
  setScenario: (s: string) => void;
}

const Ctx = createContext<AppCtx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const { data: meta } = useMeta();
  const [params, setParams] = useSearchParams();

  const [book, setBook] = useState(params.get("book") || "Soros");
  const [date, setDate] = useState(params.get("date") || "");
  const [scenario, setScenario] = useState(params.get("set") || "HistFull");

  const dates = meta?.dates ?? [];
  const scenarioSets = meta?.scenario_sets ?? [];

  // once meta lands, default the date to the latest available (if not already URL-set)
  useEffect(() => {
    if (!date && dates.length) setDate(dates[dates.length - 1]);
  }, [dates, date]);

  // reflect context into the URL (shareable), without stomping other params
  useEffect(() => {
    const next = new URLSearchParams(params);
    next.set("book", book);
    if (date) next.set("date", date);
    next.set("set", scenario);
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [book, date, scenario]);

  const value = useMemo<AppCtx>(
    () => ({
      book, date, scenario, dates, scenarioSets,
      ready: !!meta && !!date,
      setBook, setDate, setScenario,
    }),
    [book, date, scenario, dates, scenarioSets, meta],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp must be used within AppProvider");
  return v;
}
