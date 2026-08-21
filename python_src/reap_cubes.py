#!/usr/bin/env python
"""
reap_cubes.py — find (and optionally kill) Atoti cubes nobody is using.

Every cube is a JVM holding multiple GB. Three of them ran on this box at once on 2026-08-21 and
took the load average past 10, at which point every query is several times slower than the
benchmarks — which are taken on a quiet box. The cubes were not doing anything: they were
abandoned notebook kernels. JupyterLab does not stop a kernel when its tab closes, so each
"just re-run it" left another cube behind.

Two lines of defence now exist. The container culls idle kernels after 30 minutes
(docker/jupyter_config/jupyter_server_config.py). This script is the other one: it answers "is
the box clean right now?" in one command, which is the question worth asking before a demo, when
you do not want to wait out a cull interval.

    ../barra/bin/python reap_cubes.py          # list what is running, kill nothing
    ../barra/bin/python reap_cubes.py --yes    # kill the reapable ones

WHAT IT WILL NEVER KILL:
  * The API cube (the flexagg-api service's JVM). That one is the live system.
  * Any JVM whose owning Python process is still alive AND busy — a build in flight looks a lot
    like an abandoned cube from the outside, and killing someone's build mid-run is worse than
    leaving a stale one up. `--busy-cpu` sets how quiet a cube must be to count as abandoned.
Run it as a user who can signal the target (container kernels map to another uid: use sudo).
"""
from __future__ import annotations
import argparse
import os
import signal
import sys
import time

try:
    import psutil
except ImportError:                                    # pragma: no cover - environment guard
    sys.exit("reap_cubes.py needs psutil (it is in requirements.txt): pip install psutil")

JVM_MARKERS = ("jdk4py", "_atoti_server", "ActivePivot", "atoti")
API_UNIT_HINTS = ("risk_api", "flexagg-api", "uvicorn")


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return ""


def _is_cube_jvm(proc: psutil.Process) -> bool:
    """A Java process started by atoti — the JVM behind a cube."""
    try:
        if "java" not in proc.name().lower():
            return False
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False
    return any(marker in _cmdline(proc) for marker in JVM_MARKERS)


def _owner_chain(proc: psutil.Process) -> list[psutil.Process]:
    """The JVM's Python parent(s), nearest first — the kernel or script that started the cube."""
    chain = []
    try:
        parent = proc.parent()
        while parent is not None and len(chain) < 4:
            chain.append(parent)
            parent = parent.parent()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    return chain


def _is_python_owner(proc) -> bool:
    """Safe to signal as "the process that started this cube"? Only a live, non-init python.
    An orphaned JVM reparents to pid 1, so an unguarded "kill the parent" would target init."""
    if proc is None or proc.pid <= 1:
        return False
    try:
        return proc.is_running() and "python" in proc.name().lower()
    except Exception:
        return False


def _serves_the_api(proc: psutil.Process, chain: list[psutil.Process]) -> bool:
    """True if this cube belongs to the live API service — never reap it."""
    texts = [_cmdline(proc)] + [_cmdline(p) for p in chain]
    return any(hint in text for text in texts for hint in API_UNIT_HINTS)


def _ports(proc: psutil.Process) -> list[int]:
    try:
        return sorted({c.laddr.port for c in proc.net_connections(kind="inet")
                       if c.status == psutil.CONN_LISTEN})
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return []


def survey(busy_cpu: float) -> list[dict]:
    """Every cube JVM on the box, with why it is or is not reapable."""
    found = []
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            if not _is_cube_jvm(proc):
                continue
            chain = _owner_chain(proc)
            owner = chain[0] if chain else None
            rss_gb = proc.memory_info().rss / 1024 ** 3
            age_s = time.time() - proc.create_time()
            cpu = proc.cpu_percent(interval=0.3)
            if _serves_the_api(proc, chain):
                verdict, why = "keep", "the live API cube"
            elif not _is_python_owner(owner):
                # Orphaned: the kernel/script that built it died and the JVM reparented to init.
                verdict, why = "reap", "orphaned — no live python owner"
            elif cpu > busy_cpu:
                verdict, why = "keep", f"busy ({cpu:.0f}% cpu) — a build may be running"
            else:
                verdict, why = "reap", f"idle ({cpu:.0f}% cpu), owner pid {owner.pid}"
            found.append({"proc": proc, "owner": owner, "rss_gb": rss_gb, "age_s": age_s,
                          "ports": _ports(proc), "verdict": verdict, "why": why})
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually kill the reapable cubes")
    ap.add_argument("--busy-cpu", type=float, default=5.0, metavar="PCT",
                    help="above this %%CPU a cube counts as busy and is kept (default 5)")
    args = ap.parse_args()

    cubes = survey(args.busy_cpu)
    if not cubes:
        print("no Atoti cubes running.")
        return 0

    print(f"{'PID':>8}  {'RSS':>7}  {'AGE':>7}  {'PORTS':<12} {'VERDICT':<7} WHY")
    for c in cubes:
        ports = ",".join(str(p) for p in c["ports"]) or "-"
        age = f"{c['age_s'] / 60:.0f}m" if c["age_s"] < 5400 else f"{c['age_s'] / 3600:.1f}h"
        print(f"{c['proc'].pid:>8}  {c['rss_gb']:>6.1f}G  {age:>7}  {ports:<12} "
              f"{c['verdict']:<7} {c['why']}")

    reapable = [c for c in cubes if c["verdict"] == "reap"]
    freed = sum(c["rss_gb"] for c in reapable)
    if not reapable:
        print("\nnothing to reap — the box is clean.")
        return 0
    if not args.yes:
        print(f"\n{len(reapable)} reapable, {freed:.1f}G held. Re-run with --yes to kill them.")
        return 0

    for c in reapable:
        pid, owner = c["proc"].pid, c["owner"]
        # Kill the OWNER (the kernel/script) where we can: killing the JVM alone leaves the Python
        # side holding a dead session, and a kernel will happily start another one. But signal it
        # ONLY if it is really a python owner: an orphaned JVM is reparented to pid 1, and
        # "kill the parent" would then mean killing init. Fall back to the JVM itself.
        target = owner if _is_python_owner(owner) else c["proc"]
        try:
            os.kill(target.pid, signal.SIGTERM)
            print(f"killed pid {target.pid} (cube jvm {pid})")
        except PermissionError:
            print(f"pid {target.pid}: permission denied — container kernels need sudo")
        except ProcessLookupError:
            print(f"pid {target.pid}: already gone")
    print(f"\nreaped {len(reapable)}, ~{freed:.1f}G freed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
