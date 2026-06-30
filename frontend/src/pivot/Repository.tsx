// Saved-views Repository (docs/vite-ui-plan.md §5/§6): list the Public/Private tree, load a view's
// state into the workspace, save the current config, delete. Folders are real nested dirs on disk;
// this renders them recursively. Backed by the /views CRUD wrapper over views_repo.
import { useCallback, useEffect, useState } from "react";
import { listViews, loadView, saveView, deleteView } from "../api/views";
import type { ViewTree, ViewState } from "../api/types";

export function Repository({
  currentState, onLoad,
}: { currentState: ViewState; onLoad: (s: ViewState, name: string) => void }) {
  const [sections, setSections] = useState<Record<string, ViewTree>>({});
  const [name, setName] = useState("");
  const [folder, setFolder] = useState("Public");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try { setSections((await listViews()).sections); }
    catch (e) { setErr((e as Error).message); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  async function save() {
    if (!name.trim()) { setErr("name the view"); return; }
    setBusy(true); setErr(null);
    try { await saveView(name.trim(), folder, currentState); await refresh(); }
    catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  async function open(file: string) {
    try { const doc = await loadView(file); onLoad(doc.state, doc.name); }
    catch (e) { setErr((e as Error).message); }
  }
  async function remove(file: string) {
    try { await deleteView(file); await refresh(); }
    catch (e) { setErr((e as Error).message); }
  }

  return (
    <div style={{ width: "16rem", flexShrink: 0, borderLeft: "1px solid var(--line)", paddingLeft: "0.8rem" }}>
      <b style={{ fontSize: 13 }}>Repository</b>
      <div style={{ margin: "0.5rem 0" }}>
        <input type="text" placeholder="view name" value={name} onChange={(e) => setName(e.target.value)}
          style={{ width: "100%", marginBottom: "0.3rem" }} />
        <div className="row">
          <select value={folder} onChange={(e) => setFolder(e.target.value)} style={{ flex: 1 }}>
            <option value="Public">Public</option>
            <option value="Private">Private</option>
          </select>
          <button className="primary" onClick={save} disabled={busy}>Save</button>
        </div>
      </div>
      {err && <div className="err small">{err}</div>}
      {Object.entries(sections).map(([sec, tree]) => (
        <div key={sec} style={{ marginTop: "0.6rem" }}>
          <div className="muted" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.06em" }}>{sec}</div>
          <TreeView tree={tree} onOpen={open} onDelete={remove} />
        </div>
      ))}
    </div>
  );
}

function TreeView({ tree, onOpen, onDelete, depth = 0 }: {
  tree: ViewTree; onOpen: (f: string) => void; onDelete: (f: string) => void; depth?: number;
}) {
  return (
    <div style={{ paddingLeft: depth ? 10 : 0 }}>
      {Object.entries(tree.folders).map(([fname, sub]) => (
        <div key={fname}>
          <div className="small muted">📁 {fname}</div>
          <TreeView tree={sub} onOpen={onOpen} onDelete={onDelete} depth={depth + 1} />
        </div>
      ))}
      {tree.views.map((v) => (
        <div key={v.file} className="row small" style={{ justifyContent: "space-between" }}>
          <a onClick={() => onOpen(v.file)} style={{ cursor: "pointer" }}>{v.name}</a>
          <button onClick={() => onDelete(v.file)} style={{ border: "none", color: "var(--faint)", padding: "0 0.2rem" }}>×</button>
        </div>
      ))}
      {!Object.keys(tree.folders).length && !tree.views.length && depth === 0 && (
        <div className="muted small">empty</div>
      )}
    </div>
  );
}
