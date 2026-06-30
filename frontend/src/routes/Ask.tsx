// Ask lens (render_ask): free-text desk question answered by the model via its one tool (query_cube).
// The stream interleaves `> 🔎 query_cube …` progress lines — kept visible (the markdown renders them
// as blockquotes), so the grounding is auditable. Streamed, no token spend until asked.
import { useCallback, useRef, useState } from "react";
import { streamMarkdown } from "../api/stream";
import { Markdown } from "../components/Markdown";

export function Ask() {
  const [question, setQuestion] = useState("");
  const [notes, setNotes] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async () => {
    if (!question.trim()) { setErr("ask a question"); return; }
    setBusy(true); setErr(null); setText("");
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      await streamMarkdown("/ask", { question, notes: notes || undefined }, setText, ac.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setErr((e as Error).message);
    } finally { setBusy(false); }
  }, [question, notes]);

  return (
    <main className="lens">
      <h1>Ask the risk model</h1>
      <p className="sub">Free-text desk question — the model pulls its own cube slices (one guarded tool)</p>

      <div style={{ maxWidth: "46rem" }}>
        <textarea value={question} onChange={(e) => setQuestion(e.target.value)}
          placeholder="e.g. Which sector contributes most to Total VaR on the latest date?"
          rows={2}
          style={{ width: "100%", font: "inherit", padding: "0.4rem", border: "1px solid var(--line)", borderRadius: 2 }}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run(); }} />
        <input type="text" value={notes} onChange={(e) => setNotes(e.target.value)}
          placeholder="optional desk context"
          style={{ width: "100%", marginTop: "0.4rem" }} />
        <div className="row" style={{ marginTop: "0.5rem" }}>
          <button className="primary" onClick={run} disabled={busy}>{busy ? "thinking…" : "Ask"}</button>
          {busy && <button onClick={() => abortRef.current?.abort()}>stop</button>}
          <span className="muted small">⌘/Ctrl+Enter</span>
          {err && <span className="err small">{err}</span>}
        </div>
      </div>

      {text && <div style={{ marginTop: "1.2rem" }}><Markdown text={text} /></div>}
    </main>
  );
}
