// Persistent left rail of lenses. Overview is the monitor; everything else is a lens reached on
// demand (docs/vite-ui-plan.md layout). The active route keeps the accent marker.
import { NavLink } from "react-router-dom";

const LENSES: { to: string; label: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/pivot", label: "Pivot" },
  { to: "/trends", label: "Trends" },
  { to: "/stress", label: "Stress" },
  { to: "/whatif", label: "What-if" },
  { to: "/liquidity", label: "Liquidity" },
  { to: "/universe", label: "Universe" },
  { to: "/drift", label: "Drift" },
  { to: "/attribution", label: "Attribution" },
  { to: "/changes", label: "Changes" },
  { to: "/checks", label: "Checks" },
  { to: "/ask", label: "Ask" },
];

export function LeftRail() {
  return (
    <nav className="rail">
      {LENSES.map((l) => (
        <NavLink key={l.to} to={l.to} end={l.to === "/"}
          className={({ isActive }) => (isActive ? "active" : "")}>
          {l.label}
        </NavLink>
      ))}
    </nav>
  );
}
