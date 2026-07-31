// Smoke tests for the Model lens's rolling-bias chart: acceptance band, line colour flips red
// when the latest b breaches the band. Pure-render — no API.
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { BiasChart } from "./Model";
import type { ValidationSeries } from "../api/types";

function series(bs: number[]): ValidationSeries {
  return {
    bias: bs.map((b, i) => ({ date: `20${20 + Math.floor(i / 12)}-${String((i % 12) + 1).padStart(2, "0")}-28`, b })),
    band: 0.289, exceedance_2s: 0.05, n_months: bs.length,
  };
}

describe("BiasChart", () => {
  it("draws the acceptance band and an ink line when calibrated", () => {
    const { container, getByText } = render(<BiasChart s={series([0.9, 1.0, 1.1, 0.95])} label="book" />);
    expect(container.querySelector("rect")).toBeTruthy();          // the shaded band
    const line = container.querySelector("path");
    expect(line?.getAttribute("stroke")).toBe("#111");
    getByText("book");
    getByText(/b 0\.95/);
  });

  it("turns the line red when the latest b breaches the band", () => {
    const { container } = render(<BiasChart s={series([1.0, 1.1, 1.3, 1.5])} label="book" />);
    expect(container.querySelector("path")?.getAttribute("stroke")).toBe("#a8322a");
  });

  it("renders a placeholder for <2 points", () => {
    const { getByText } = render(<BiasChart s={series([1.0])} label="book" />);
    getByText("insufficient history");
  });
});
