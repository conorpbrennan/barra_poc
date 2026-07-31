import { describe, it, expect } from "vitest";
import { pct, signedPct, num, ragClass, ragLabel, days } from "./format";

describe("format", () => {
  it("pct scales fractions to %", () => {
    expect(pct(0.035)).toBe("3.50%");
    expect(pct(0.035, 1)).toBe("3.5%");
    expect(pct(null)).toBe("—");
  });
  it("signedPct prefixes +", () => {
    expect(signedPct(0.01)).toBe("+1.00%");
    expect(signedPct(-0.01)).toBe("-1.00%");
  });
  it("num and days", () => {
    expect(num(1.2345, 3)).toBe("1.234");
    expect(days(2.2)).toBe("2.2d");
  });
  it("maps the three RAG vocabularies to one class", () => {
    expect(ragClass("green")).toBe("rag-green");
    expect(ragClass("pass")).toBe("rag-green");
    expect(ragClass("amber")).toBe("rag-amber");
    expect(ragClass("warn")).toBe("rag-amber");
    expect(ragClass("breach")).toBe("rag-red");
    expect(ragClass("fail")).toBe("rag-red");
    expect(ragLabel("breach")).toBe("breach");
  });
});
