import { Suspense, lazy } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { AppProvider } from "./context/AppContext";
import { ContextBar } from "./shell/ContextBar";
import { LeftRail } from "./shell/LeftRail";
import { Overview } from "./routes/Overview";
// The pivot pulls AG Grid (+ Vega in chart mode) — code-split so the monitor loads light.
const Pivot = lazy(() => import("./routes/Pivot").then((m) => ({ default: m.Pivot })));
import { Trends } from "./routes/Trends";
import { Stress } from "./routes/Stress";
import { WhatIf } from "./routes/WhatIf";
import { Universe } from "./routes/Universe";
import { Drift } from "./routes/Drift";
import { Attribution } from "./routes/Attribution";
import { Changes } from "./routes/Changes";
import { Ask } from "./routes/Ask";
import { Checks } from "./routes/Checks";
import { Model } from "./routes/Model";

export default function App() {
  return (
    <AppProvider>
      <div className="app">
        <ContextBar />
        <div className="body">
          <LeftRail />
          <Suspense fallback={<main className="lens"><div className="spin">loading…</div></main>}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/pivot" element={<Pivot />} />
            <Route path="/trends" element={<Trends />} />
            <Route path="/stress" element={<Stress />} />
            <Route path="/whatif" element={<WhatIf />} />
            <Route path="/universe" element={<Universe />} />
            <Route path="/drift" element={<Drift />} />
            <Route path="/attribution" element={<Attribution />} />
            <Route path="/changes" element={<Changes />} />
            <Route path="/ask" element={<Ask />} />
            <Route path="/model" element={<Model />} />
            <Route path="/checks" element={<Checks />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          </Suspense>
        </div>
      </div>
    </AppProvider>
  );
}
