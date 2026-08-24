"""
notebook_helpers.py — tiny shared utilities for the direct-Atoti demo notebook
(`notebooks/13f_risk.ipynb`, one notebook for every manager — see `manager_picker`).

This is deliberately NOT a view interpreter. The notebook's whole point is to show the Atoti
Python API directly: every view is reconstructed as explicit, literal `cube.query(...)` calls in
its own cell. All this module does is (1) build the cube once and (2) keep the 8 grid cells short
by centralising the pandas-Styler formatting. The query logic stays in the notebook, visible.

Run the notebook (and anything importing this) with `PYTHONPATH=python_src` so the bare imports
below resolve — never prefix imports with `python_src`.
"""
from __future__ import annotations
import atexit
import socket
import time
import pandas as pd
from barra_factor_risk_cube import load_frames, build_cube

# A free port: the standalone Atoti UI owns :9090 and risk_api uses :9091/:9095, so the notebook's
# own session takes :9096. (We do not expose this web app — the notebook is the only surface.)
CUBE_PORT = 9096

_BUILT: tuple | None = None          # this kernel's session, if it already has one
BUILD_SECONDS: float | None = None   # wall-clock of the last real build (None until one runs)


def _port_owner(port: int) -> str:
    """Best-effort "pid 12345 (python3.12)" for whoever is listening on `port`; "" if unknown
    (psutil absent, or the socket belongs to a process this one cannot see)."""
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port and conn.pid:
                return f"pid {conn.pid} ({psutil.Process(conn.pid).name()})"
    except Exception:
        pass
    return ""


def _session_port(session) -> int:
    """The session's port from `Session.url` (`Session.port` is deprecated in atoti 0.9.15)."""
    from urllib.parse import urlparse
    return int(urlparse(session.url).port)


def _alive(port: int) -> bool:
    """True if something accepts connections on `port` — the cheap check that this kernel's JVM
    is still there before its cached session is reused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port(port: int, tries: int = 20) -> int:
    """First free port at or above `port`. Atoti's server binds every interface, so probe the
    same way — a loopback-only probe reports 9096 free while another kernel's JVM holds it.

    Stepping to the next port keeps the build working, but it also HIDES the reason: some other
    kernel left a cube running. So say so. An abandoned cube costs multiple GB in a 12g container
    and slows every query on the box, and the first line of the notebook is where you would want
    to find out — not twenty minutes later wondering why the demo feels slow."""
    for candidate in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # SO_REUSEADDR, as the JVM's own server socket sets it: a port whose only occupant is
            # a TIME_WAIT left by a dead cube's connections is free, not "held by another cube"
            # (2026-08-21 — the probe stepped to :9097 for no reason after an interrupted build).
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", candidate))
                if candidate != port:
                    owner = _port_owner(port)
                    print(f":{port} is held by another cube{' — ' + owner if owner else ''};"
                          f" using :{candidate}. Reap it with python_src/reap_cubes.py")
                return candidate
            except OSError:
                continue
    raise RuntimeError(f"no free port in {port}..{port + tries - 1} for the notebook cube")


def _build_cube_interrupt_safe(port: int):
    """Start the cube with SIGINT IGNORED in this process while the JVM is spawned.

    JupyterLab's Interrupt (the Stop button) sends SIGINT to the kernel's whole PROCESS GROUP,
    and the Atoti JVM is a child in that group — so every interrupt, even of an unrelated slow
    cell, shut the cube down (2026-08-21: three cubes lost to Stop presses in one session).
    HotSpot keeps an INHERITED ignored SIGINT ignored (it installs no handler for a signal that
    was SIG_IGN at start), so a JVM spawned while the kernel ignores SIGINT is immune to
    Interrupt for its whole life; the kernel's own handler is restored right after, so Stop
    still interrupts Python cells. Restart Kernel still tears the cube down (SIGTERM/SIGKILL).
    Measured: a group SIGINT kills a default-spawned JVM and leaves an ignore-spawned one up.
    Falls back to a plain start outside the main thread (signal handlers are main-thread only)."""
    try:
        import signal, threading
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("not main thread")
        prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        return build_cube(load_frames(), port=port)
    try:
        return build_cube(load_frames(), port=port)
    finally:
        signal.signal(signal.SIGINT, prev)


def build(port: int = CUBE_PORT, *, force: bool = False):
    """Build the factor-risk cube from the six parquet frames and return (session, cube).

    Safe to call more than once. The build is the slow step (~1–2 min on a warm tmp/ cache), so
    the first call in a kernel does it and every later call hands back the SAME live session —
    re-running the build cell is then instant instead of raising "Address already in use" against
    the session this kernel already started. Pass `force=True` for a genuinely fresh build (e.g.
    after re-running a builder), which drops the cached session first.

    A port held by a DIFFERENT kernel — the usual case, since closing a JupyterLab tab leaves its
    kernel and its JVM running — no longer fails either: the next free port is used instead. The
    notebook is the only surface on this session, so which port it lands on does not matter.

        session, cube = build()
        h, l, m = cube.hierarchies, cube.levels, cube.measures
    """
    global _BUILT, BUILD_SECONDS
    if _BUILT is not None and not force and not _alive(_session_port(_BUILT[0])):
        # Interrupting the kernel (Stop / Kernel -> Interrupt) kills the Atoti JVM but leaves the
        # Python-side session object behind; handing it back again gives every query
        # "ConnectError: [Errno 111] Connection refused" (2026-08-21). Rebuild instead.
        print(f"this kernel's cube on :{_session_port(_BUILT[0])} is gone (JVM died — an interrupt?); rebuilding")
        _BUILT = None
    if _BUILT is not None and not force:                            # re-run of the build cell
        print(f"reusing this kernel's cube on :{_session_port(_BUILT[0])} "
              f"(built in {BUILD_SECONDS:.1f}s; no rebuild)")
        return _BUILT
    if _BUILT is not None:                       # force=True: let the old one go before rebinding
        _BUILT = None
    port = _free_port(port)
    t0 = time.perf_counter()
    session, cube = _build_cube_interrupt_safe(port)
    # The notebook container is jailed to 2 CPUs, and on the 11-manager frames the scenario-vector
    # queries (e.g. an Evt window's Scenario PnL) can exceed ActivePivot's 30s default query time
    # limit there — the host API cube on all cores never hits it. Raised for this session only.
    cube.shared_context["queriesTimeLimit"] = 180
    BUILD_SECONDS = time.perf_counter() - t0
    # Worth printing: this is the one slow step, and the number is the fastest way to tell a
    # healthy build (~20s: arrow cache hit) from a degraded one (~24s: cache miss, rewriting) or
    # a loaded box (queries after it will be slow too). Set BARRA_CUBE_TIMINGS=1 for the stages.
    print(f"cube load: {BUILD_SECONDS:.1f}s on :{port}")
    _BUILT = (session, cube)
    atexit.register(_close)     # deterministic teardown; see _close
    return session, cube


def _close() -> None:
    """Close the cached session at interpreter exit. Holding the session in a module global (so
    the build cell is re-runnable) keeps it alive into interpreter finalisation, where atoti's own
    `Session.__del__` tears down a subprocess against already-finalising buffers and Python aborts
    with `_enter_buffered_busy: could not acquire lock ... at interpreter shutdown`. Harmless in a
    kernel, but in a script it fires AFTER the exit code is set and replaces it with SIGABRT —
    which would turn a green `test_notebook.py` into a failed command. Closing first avoids it."""
    global _BUILT
    if _BUILT is None:
        return
    session, _cube = _BUILT
    _BUILT = None
    try:
        session.close()
    except Exception:           # teardown is best-effort: never mask a real exit status
        pass


# Anchor colours of matplotlib's "Blues" (ColorBrewer), so the pure-python ramp below matches the
# app's look without importing matplotlib — the notebook container is air-gapped and ships only
# pure-python libs (altair/narwhals staged at data/_pylibs), so Styler.background_gradient's
# matplotlib dependency is exactly the thing we can't have.
_BLUES = [(247, 251, 255), (198, 219, 239), (107, 174, 214), (33, 113, 181), (8, 48, 107)]


def _blues_css(s: pd.Series) -> list[str]:
    """Per-column CSS for a Blues heatmap: min→lightest, max→darkest, NaN→unstyled. Text flips to
    white on dark cells (same intent as pandas' text_color_threshold)."""
    v = pd.to_numeric(s, errors="coerce")
    lo, hi = v.min(), v.max()
    if pd.isna(lo) or hi == lo:
        return [""] * len(s)
    out = []
    for x in (v - lo) / (hi - lo):
        if pd.isna(x):
            out.append("")
            continue
        seg = min(int(x * (len(_BLUES) - 1)), len(_BLUES) - 2)
        t = x * (len(_BLUES) - 1) - seg
        r, g, b = (round(a + (b_ - a) * t) for a, b_ in zip(_BLUES[seg], _BLUES[seg + 1]))
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
        out.append(f"background-color: rgb({r},{g},{b}); color: {'#f1f1f1' if lum < 0.45 else '#111'}")
    return out


def style_grid(df: pd.DataFrame, *, pct: bool = True, prec: int = 3, money=None):
    """Return a pandas Styler reproducing the app's grid look functionally: a per-column blue
    heatmap over the numeric measure columns + percent/fixed formatting + an em-dash for nulls.

    `pct`/`prec` mirror a view's `as_pct`/`prec` state. Every Soros 13F grid is as_pct with
    prec=3, so the defaults match; pass pct=False for a plain fixed-decimal grid.

    Dollar columns — the cube's "<measure> $" twins, `Market value`, `Manager MV` — are formatted
    as whole dollars with separators ($151,470,121) regardless of `pct`; `money` names extra
    columns to treat the same way (the UI's units=dollar view).
    """
    num = list(df.select_dtypes("number").columns)
    fmt = (f"{{:.{prec}%}}" if pct else f"{{:.{prec}f}}")
    dollar = {c for c in num if str(c).endswith(" $") or c in ("Market value", "Manager MV")}
    dollar |= set(money or [])
    formats = {c: ("${:,.0f}" if c in dollar else fmt) for c in num}
    return (df.style
              .apply(_blues_css, subset=num, axis=0)   # per-column heatmap, like the app
              .format(formats, na_rep="—"))


# ---------------------------------------------------------------------------
# Manager selection
# ---------------------------------------------------------------------------
# The demo notebooks were one-per-manager, identical apart from the manager token — so a new
# manager meant copying 43 cells and hand-editing 30 of them, and one mistyped name silently
# produced empty views (`l["Manager"] == "Sorros"` does not raise; it matches nothing). One
# notebook + a dropdown replaces both problems: the name can only ever be one the data has.

_MANAGER_NAMES: list[str] | None = None


def manager_names(folder=None) -> list[str]:
    """Every manager the cube can actually price, sorted. Memoized per kernel.

    Read from the `positions` frame's Manager column — the same source `/meta.managers` uses, and
    deliberately NOT a cube query, for two measured reasons (123-manager build, this box):

      * it is right. The cube's Manager LEVEL has 124 members, not 123: the optional `managers`
        frame partial-joins onto Positions, so a manager with metadata but no position facts is a
        level member. MetLife is exactly that (its 13F is 6 equity CUSIPs ever, an empty equity
        portfolio). Offering it in a picker would hand back empty views for every cell — the
        silent-empty failure the picker exists to prevent.
      * it is ~35x faster. Enumerating the level costs 17.0s via `contributors.COUNT`, 20.9s via
        `Manager n positions`, 45.8s via `Manager MV`; a single-column parquet read is 0.49s.
    """
    global _MANAGER_NAMES
    if _MANAGER_NAMES is None:
        from barra_factor_risk_cube import OUT
        col = pd.read_parquet((folder or OUT) / "positions.parquet", columns=["Manager"])
        _MANAGER_NAMES = sorted(col["Manager"].astype(str).unique())
    return list(_MANAGER_NAMES)


class ManagerSelection:
    """The notebook's current manager, as a live cube filter.

    Read `.filter` (or just `&` it, which is the same thing) at cell-run time rather than binding
    a condition once, so the dropdown never has to reach back into the notebook's globals: the
    selection is held here, and every cell below picks it up on its next run.

        MGR = N.manager_picker(cube, "Citadel")
        cube.query(m["Total VaR 99"], filter=MGR & (l["Date"] == D))
        ... f"VaR trend - {MGR.name}"
    """

    def __init__(self, cube, name: str, names: list[str]):
        self._levels = cube.levels
        self._names = names
        self.name = name
        self.widget = None          # the Dropdown, when one was rendered (None headless)

    @property
    def filter(self):
        """`l["Manager"] == <current selection>`, rebuilt on every read."""
        return self._levels["Manager"] == self.name

    def __and__(self, other):       # so `MGR & (l["Date"] == D)` reads like the condition it is
        return self.filter & other

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f'Manager == "{self.name}"  ({len(self._names)} loaded)'


_SELECTION: "ManagerSelection | None" = None


def manager_picker(cube, default: str, *, managers: list[str] | None = None,
                   quiet: bool = False, reset: bool = False) -> ManagerSelection:
    """Show a manager dropdown and return the live `ManagerSelection` the cells below read.

    Changing the dropdown updates the selection immediately, but **cells that already ran keep
    the output they produced** — re-run them (Run ▸ Run All Below) to re-point them. The
    selection survives a re-run of this cell too, so a plain `Run All` no longer resets it;
    pass `reset=True` to force back to `default`.

    `default` is required rather than defaulted here on purpose: the notebook is the one place
    the starting manager is written, so there is no second value to drift from it.
    `managers` restricts the options to a curated subset; the default is every manager in the
    frames. Selecting a name updates the selection in place — the cells below then need a re-run
    (Run > Run All Below), which is the one thing a widget cannot do for you.

    Degrades to a plain validated selection when ipywidgets is missing, so the same notebook still
    executes headlessly (the render script, `test_notebook.py`) and in the container until its
    image carries ipywidgets. Either way an unknown `default` raises here rather than silently
    matching nothing downstream.
    """
    global _SELECTION
    names = list(managers) if managers else manager_names()

    # Re-running the setup cell must NOT silently throw away the manager you picked. `Run All`
    # (as opposed to `Run All Below`) re-executes this cell, and the first version of this
    # function rebuilt the selection at `default` every time — so the cells below went back to
    # Citadel while the dropdown appeared to say otherwise. Same reasoning as `build()` holding
    # its session in a module global: the cell is re-runnable, so what it owns has to survive it.
    start = default
    if _SELECTION is not None and not reset and _SELECTION.name in names:
        start = _SELECTION.name

    if default not in names:
        near = [n for n in names if n.lower().startswith(default[:3].lower())]
        raise ValueError(f"unknown manager {default!r}; {len(names)} loaded"
                         + (f" — did you mean {near}?" if near else f" (e.g. {names[:5]})"))
    sel = ManagerSelection(cube, start, names)
    _SELECTION = sel
    if start != default and not quiet:
        print(f"kept your previous selection: {start} (pass reset=True to go back to {default})")

    try:
        import ipywidgets as widgets
        from IPython.display import display
    except ImportError:            # headless execution, or an image without the widget stack
        if not quiet:
            print(f"manager: {start}  (ipywidgets absent — no picker; {len(names)} available)")
        return sel

    dropdown = widgets.Dropdown(options=names, value=start, description="Manager:",
                                layout=widgets.Layout(width="22rem"))
    note = widgets.HTML(_picker_note(start, changed=False))

    def _on_change(change):
        sel.name = change["new"]
        note.value = _picker_note(change["new"], changed=True)

    dropdown.observe(_on_change, names="value")
    sel.widget = dropdown
    display(widgets.VBox([dropdown, note]))
    return sel


def _picker_note(name: str, *, changed: bool) -> str:
    """The one line under the dropdown. Grey, no box — it is a status, not a control."""
    msg = (f"now <b>{name}</b> — re-run the cells below (Run &gt; Run All Below)"
           if changed else f"every cell below reads <b>{name}</b>")
    return f'<div style="color:#6b6b63;font-size:0.85em;padding-left:0.4rem">{msg}</div>'
