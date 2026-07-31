// On-demand streamed LLM commentary: a button that, when clicked, POSTs to a streaming endpoint
// and renders the markdown as deltas arrive (docs/vite-ui-plan.md §7). No token spend until clicked;
// the finished answer is cached by a caller-supplied key (mirrors pv_analysis_cache / pv_ask_cache)
// so re-viewing is static until "Regenerate".
import { useCallback, useRef, useState } from "react";
import { streamMarkdown } from "../api/stream";
import { Markdown } from "./Markdown";

// module-level cache, keyed by the caller's key string -> finished text
const CACHE = new Map<string, string>();

export function StreamPanel({
  path, body, cacheKey, label = "Generate commentary", regenLabel = "Regenerate",
}: {
  path: string;
  body: unknown;
  cacheKey: string;
  label?: string;
  regenLabel?: string;
}) {
  const [text, setText] = useState<string>(() => CACHE.get(cacheKey) ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setErr(null);
    setText("");
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const full = await streamMarkdown(path, body, setText, ac.signal);
      CACHE.set(cacheKey, full);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [path, body, cacheKey]);

  const has = text.length > 0;
  return (
    <div>
      <div className="row" style={{ marginBottom: "0.5rem" }}>
        <button onClick={run} disabled={busy} className={has ? "" : "primary"}>
          {busy ? "streaming…" : has ? regenLabel : label}
        </button>
        {busy && (
          <button onClick={() => abortRef.current?.abort()}>stop</button>
        )}
      </div>
      {err && <div className="err small">{err}</div>}
      {has && <Markdown text={text} />}
    </div>
  );
}
