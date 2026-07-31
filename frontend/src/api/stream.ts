// Streaming reader for the LLM panels (/analysis, /ask, /whatchanged/analysis). Those endpoints
// return RAW text/markdown — not SSE, no `data:` frames (docs/vite-ui-plan.md §7) — so we consume
// the body as a ReadableStream of UTF-8 deltas and append to a buffer, calling onDelta with the
// running text. Returns the full text; surfaces the shared 20/60s 429 as a clear message.
import { API_BASE, ApiError } from "./client";

export async function streamMarkdown(
  path: string,
  body: unknown,
  onDelta: (fullText: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = (j && (j.detail as string)) || detail;
    } catch {
      /* non-JSON error body */
    }
    if (res.status === 429) detail = "Rate limit reached (20 / 60s). Wait a moment and retry.";
    throw new ApiError(res.status, detail);
  }
  if (!res.body) {
    const text = await res.text();
    onDelta(text);
    return text;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let acc = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    acc += decoder.decode(value, { stream: true });
    onDelta(acc);
  }
  acc += decoder.decode();
  onDelta(acc);
  return acc;
}
