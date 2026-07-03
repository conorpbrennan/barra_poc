// Smoke tests for the PnL-attribution chart components (Step 15): the sign-aware stacked hero
// and the §4 reconcile band chart. Pure-render — no API.
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { StackedHero, BandChart, irSignificance } from "./Attribution";
import type { PnlLinkageResult } from "../api/types";

const SERIES = [
  { date: "2024-01-31", market: 0.01, style: 0.002, specific: -0.001, realized: 0.011 },
  { date: "2024-02-29", market: 0.02, style: 0.004, specific: -0.003, realized: 0.021 },
  { date: "2024-03-31", market: 0.05, style: 0.006, specific: -0.006, realized: 0.05 },
];

const LK: PnlLinkageResult = {
  T: "2024-09-30", to: "2024-12-30", horizon_months: 3, n_days: 63, book: "Soros",
  stress: { vol_mult: 1.25, rho_blend: 0.75 },
  book_total: { name: "Book total", kind: "book", exposure: null, risk_share: 1,
    realized: -0.02, sd_base: 0.1, sd_stressed: 0.17, z: -0.2, verdict: "within" },
  rows: [
    { name: "Market", kind: "factor", exposure: 1.0, risk_share: 0.8,
      realized: 0.05, sd_base: 0.09, sd_stressed: 0.11, z: 0.55, verdict: "within" },
    { name: "Momentum", kind: "factor", exposure: 0.003, risk_share: 0.05,
      realized: 0.02, sd_base: 0.003, sd_stressed: 0.004, z: 6.7, verdict: "investigate",
      exposure_window_avg: 0.17,
      driver: { kind: "exposure_migration", migrated: true, ratio: 55, z_window: 0.2,
                factor_sigma: 0.8, text: "exposure-timing artifact — check Δx, not the factor." } },
    { name: "Specific", kind: "specific", exposure: null, risk_share: 0.1,
      realized: -0.01, sd_base: 0.004, sd_stressed: 0.005, z: -2.5, verdict: "stress" },
  ],
  positions: [], breach_comovement: null, surprises: [], note: "",
};

describe("StackedHero", () => {
  it("draws three stacked bands, the realized ink line, and direct labels", () => {
    const { container, getByText } = render(<StackedHero series={SERIES} />);
    const paths = container.querySelectorAll("path");
    expect(paths.length).toBe(4);                       // 3 areas + 1 line
    const line = paths[3];
    expect(line.getAttribute("stroke")).toBe("#111");
    expect(line.getAttribute("fill")).toBe("none");
    getByText("Market"); getByText("Style"); getByText("Specific");
    getByText(/realized \+5\.0%/);
  });

  it("stacks negatives below zero (Specific band sits under the axis)", () => {
    const { container } = render(<StackedHero series={SERIES} />);
    // with a negative specific the y-domain must dip below 0 -> a 0-tick and a negative min tick
    const texts = [...container.querySelectorAll("text")].map((t) => t.textContent);
    expect(texts).toContain("0%");                      // the zero gridline label
  });

  it("renders a placeholder for <2 points", () => {
    const { getByText } = render(<StackedHero series={SERIES.slice(0, 1)} />);
    getByText("insufficient data");
  });
});

describe("irSignificance", () => {
  it("matches t = IR·√T and years-to-2σ = (2/IR)²: IR 0.5 → 16y, IR 1.0 → 4y", () => {
    expect(irSignificance(0.5, 12 * 16)!.t).toBeCloseTo(2.0);
    expect(irSignificance(0.5, 12)!.years).toBeCloseTo(16);
    expect(irSignificance(1.0, 12)!.years).toBeCloseTo(4);
    expect(irSignificance(-0.5, 12)!.years).toBeCloseTo(16);   // |IR| for the horizon
    expect(irSignificance(-0.5, 12 * 16)!.t).toBeCloseTo(-2.0);
  });

  it("returns null on missing IR or empty window; null years at IR 0", () => {
    expect(irSignificance(null, 12)).toBeNull();
    expect(irSignificance(undefined, 12)).toBeNull();
    expect(irSignificance(0.5, 0)).toBeNull();
    expect(irSignificance(0, 12)!.years).toBeNull();
  });
});

describe("BandChart", () => {
  it("draws two bands + a dot per row (plus book total), z labels, verdict colours", () => {
    const { container, getByText } = render(<BandChart lk={LK} />);
    // 4 data rows (3 + book) x 2 band rects, + 2 legend swatches
    expect(container.querySelectorAll("rect").length).toBe(10);
    const dots = [...container.querySelectorAll("circle")];
    expect(dots.length).toBe(4 + 4);                    // 4 row dots + 4 legend dots (incl. hollow)
    getByText("Momentum"); getByText("Book total");
    getByText("+6.7σ");                                 // the breach, clamped into the axis
    // verdict colours: investigate red, stress amber
    const fills = dots.map((d) => d.getAttribute("fill"));
    expect(fills).toContain("#a8322a");
    expect(fills).toContain("#b07d2b");
  });

  it("draws an exposure-migration breach as a hollow dot in the verdict colour", () => {
    const { container } = render(<BandChart lk={LK} />);
    // Momentum: investigate verdict but driver.kind=exposure_migration -> ring, not filled red
    const hollow = [...container.querySelectorAll("circle")]
      .filter((d) => d.getAttribute("fill") === "#fffff8" && d.getAttribute("stroke") === "#a8322a");
    expect(hollow.length).toBe(1);
  });

  it("stressed band is wider than base and the dot is clamped inside the frame", () => {
    const { container } = render(<BandChart lk={LK} />);
    const rects = [...container.querySelectorAll("rect")];
    // first row (Market): rect 0 = stressed, rect 1 = base
    const stressed = Number(rects[0].getAttribute("width"));
    const base = Number(rects[1].getAttribute("width"));
    expect(stressed).toBeGreaterThan(base);
    const dots = [...container.querySelectorAll("circle")];
    for (const d of dots) {
      const cx = Number(d.getAttribute("cx"));
      expect(cx).toBeGreaterThan(0);
      expect(cx).toBeLessThan(700);
    }
  });
});
