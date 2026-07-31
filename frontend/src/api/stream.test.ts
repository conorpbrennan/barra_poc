import { describe, it, expect, vi, afterEach } from "vitest";
import { streamMarkdown } from "./stream";

function streamResponse(chunks: string[]): Response {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/markdown" } });
}

afterEach(() => vi.restoreAllMocks());

describe("streamMarkdown", () => {
  it("appends UTF-8 deltas and returns the full text", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(streamResponse(["Hello ", "risk ", "desk"])));
    const seen: string[] = [];
    const full = await streamMarkdown("/ask", { question: "x" }, (t) => seen.push(t));
    expect(full).toBe("Hello risk desk");
    expect(seen[seen.length - 1]).toBe("Hello risk desk");
    expect(seen.length).toBeGreaterThan(1);          // streamed, not one-shot
    expect(seen[0]).toBe("Hello ");                  // first delta
  });

  it("surfaces the 20/60s rate limit clearly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "analysis rate limit reached" }), { status: 429 })));
    await expect(streamMarkdown("/ask", {}, () => {})).rejects.toThrow(/rate limit/i);
  });
});
