import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Sparkline, BulletGraph } from "./svg";

describe("svg primitives", () => {
  it("Sparkline draws a path for >=2 points", () => {
    const { container } = render(<Sparkline values={[1, 2, 1.5, 3]} />);
    const path = container.querySelector("path");
    expect(path).toBeTruthy();
    expect(path?.getAttribute("d")).toMatch(/^M/);
  });

  it("Sparkline renders empty for <2 points (no crash)", () => {
    const { container } = render(<Sparkline values={[null, 1]} />);
    expect(container.querySelector("svg")).toBeTruthy();
  });

  it("BulletGraph draws the measure bar + a limit marker", () => {
    const { container } = render(<BulletGraph value={0.03} warn={0.04} limit={0.05} status="green" />);
    const rects = container.querySelectorAll("rect");
    const lines = container.querySelectorAll("line");
    expect(rects.length).toBeGreaterThanOrEqual(2);   // band + measure bar
    expect(lines.length).toBeGreaterThanOrEqual(1);   // limit marker
  });

  it("BulletGraph colours a breach red", () => {
    const { container } = render(<BulletGraph value={0.06} warn={0.04} limit={0.05} status="breach" />);
    const bar = container.querySelectorAll("rect")[container.querySelectorAll("rect").length - 1];
    expect(bar.getAttribute("fill")).toBe("#a3322b");
  });
});
