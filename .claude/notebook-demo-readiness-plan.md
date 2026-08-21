# Plan — notebook demo readiness

**Written** 2026-08-21 · **Demo** 2026-08-24 (ActiveViam, Kathy Perrotte / Shelly)
**Branch** `feat/buyside-124-books`

## Why

`notebooks/soros_13f_risk.ipynb` is the reproducibility lens of the demo — the deck's
slide 12 argues "a quant who distrusts the screen reproduces it in a cell". Right now the
notebook **runs green but does not look it**:

- Cell 3 carries a saved `BadArgumentException` / `TimeoutException` Java stack trace.
- That trace is **pre-optimisation**: it queries `Scenario PnL at day` over
  `[ScenarioDay].Members` (the legacy day-dimension walk, the 11s path retired on
  2026-08-15) at the stale as-of date `2024-12-31`.
- The cell's *source* was already migrated to the new physical-table path
  (`PnL at day` over `ScenarioDays.Day/DayDate`, sliced by `DaySet`). Only the stored
  output is stale.
- Execution counts are ragged — `[4, 5, None, None, 5, 6, 7, 8, 9, 10, 11, 12, None,
  None, None, 13, 14, 15, 16]` — so the outputs are a partial re-run, not a clean pass.

Verified 2026-08-21 by `test_notebook.py`: **6 passed, 0 failed**, including
`t_covid_path_unpacks_vector` (the optimised per-day path) and `t_latest_cob_is_D`
(confirms `D = dt.date(2026, 6, 30)` is current). Cube built clean: 6,004,074 leaf rows,
7 scenario sets, 123 PIT sets, 68,425 scenario-day rows.

**Conclusion (WRONG — corrected 2026-08-21 after execution): a clean execute-and-save was not
enough.** Both notebooks also carried a real defect. See "Outcome" at the end of this file.

## Scope

In scope: making both notebooks presentable and guarded before the demo.
Out of scope: any change to the cube, `risk_api.py`, the container, or the model.

---

## Step 0 — Back up the demo artefact

```bash
cd /home/abrennan/dev/barra_poc
cp notebooks/soros_13f_risk.ipynb /tmp/soros_13f_risk.ipynb.bak-$(date +%s)
free -g          # need ~17G headroom; box has 62G
```

## Step 1 — Clean re-run of `soros_13f_risk.ipynb`

Execute top to bottom on a fresh kernel so every cell has a sequential execution count
and no error output survives.

```bash
cd /home/abrennan/dev/barra_poc
ATOTI_HIDE_EULA_MESSAGE=True PYTHONPATH=/home/abrennan/dev/barra_poc/python_src \
  barra/bin/jupyter nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.timeout=1800 \
  notebooks/soros_13f_risk.ipynb
```

Notes:
- `--inplace` rewrites the file. Step 0 backup is not optional.
- Timeout 1800s: the cube build is the slow step (~1-2 min warm); scenario cells are
  seconds each on the optimised path.
- The notebook builds its **own** cube on `:9096` (`notebook_helpers.CUBE_PORT`). The API
  cube on `:9095` is unaffected and need not be stopped.

### Acceptance

```bash
barra/bin/python - <<'PY'
import json, pathlib
nb = json.loads(pathlib.Path("notebooks/soros_13f_risk.ipynb").read_text())
errs = [i for i, c in enumerate(nb["cells"])
        if c["cell_type"] == "code"
        for o in c.get("outputs", []) if o.get("output_type") == "error"]
ec = [c.get("execution_count") for c in nb["cells"] if c["cell_type"] == "code"]
print("error cells:", errs or "none")
print("execution_counts:", ec)
assert not errs, "still has error output"
assert ec == list(range(1, len(ec) + 1)), "execution counts not sequential"
print("OK")
PY
```

Both must hold: zero error outputs, execution counts `1..N` in order.

---

## Step 2 — Re-run the guard, confirm still green

```bash
cd /home/abrennan/dev/barra_poc/python_src && ATOTI_HIDE_EULA_MESSAGE=True \
  PYTHONPATH=/home/abrennan/dev/barra_poc/python_src \
  ../barra/bin/python test_notebook.py
```

Expect `6 passed, 0 failed`. `t_latest_cob_is_D` parses `D` out of the notebook source,
so if the re-run altered `D` this catches it.

---

## Step 3 — `vanguard_13f_risk.ipynb`

**Currently unguarded.** `test_notebook.py` mirrors only the Soros notebook
(`NOTEBOOK = ... / "soros_13f_risk.ipynb"`). The Vanguard notebook is the same shape —
35 cells, 19 code cells — but nothing verifies it and it was not checked on 2026-08-21.

1. Scan its saved outputs for errors (acceptance script above, different path).
2. Execute it the same way as Step 1.
3. Decide whether to open it at the demo. **If it is not going on screen, say so and
   skip it** — an unguarded notebook is a liability, not an asset.

Optional, only if time allows: parameterise `test_notebook.py` over both notebooks so
`NOTEBOOK` is a loop variable rather than a module constant. Low risk, but it is a code
change to a passing test three days out — defer unless Vanguard is definitely being shown.

---

## Step 4 — Commit

The repo hooks require a feature file on a feature branch. Branch is
`feat/buyside-124-books`; `features/feat-buyside-124-books.md` must exist with a real
requirement before source commits are accepted. Notebooks may fall under the docs/data
gate rather than the source gate — confirm which applies before committing rather than
fighting the hook mid-run.

```bash
git add notebooks/soros_13f_risk.ipynb        # + vanguard if executed
git commit -F - <<'MSG'
Clean re-run of demo notebooks

Clears the pre-optimisation ScenarioDay traceback frozen in cell 3 and
regularises execution counts. No source change: the query path was already
migrated to PnL at day over ScenarioDays.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
```

---

## Risks

| Risk | Mitigation |
|---|---|
| `--inplace` corrupts the demo artefact | Step 0 backup; acceptance check before commit |
| Re-run surfaces a *new* failure the 6 tests do not cover | Acceptance script asserts zero error outputs across **all** cells, not just tested ones |
| Second cube exhausts memory alongside the API cube | `free -g` first; needs ~17G, box has 62G |
| Notebook edits land in the container's `/work` mid-demo | `notebooks/` is mounted **rw** into the container — do not re-run while a guest is connected |

---

## Related items found 2026-08-21 — NOT in this plan

Recorded so they are not lost. None are notebook work.

1. **`risk_api.py` model pin** updated `claude-opus-4-8` -> `claude-opus-5` (6 call sites)
   plus 3 refs in `CLAUDE.md`. **The service has not been restarted**, so the running API
   still uses the old model.
2. **Refusal fallbacks not wired.** Opus 5 can return HTTP 200 with
   `stop_reason: "refusal"`; the `except anthropic.APIError` handlers will not catch it.
   Would need `client.beta.messages.stream` + `betas=["server-side-fallback-2026-07-01"]`.
3. **`run_all_tailscale.sh` header is stale.** It advertises the UI on `:8443` and the
   notebook on `:9443` with nginx basic-auth. `tailscale serve status` shows neither —
   the live front door is a Funnel on the bare hostname to nginx `:8090`, and
   `/jupyter/` there is **token-only, no basic-auth**.
4. **Cube binds `*:9095`, not loopback.** `run_all_tailscale.sh:10-11` claims every cube
   port "stays bound to localhost". `ss` disagrees. Check before the demo.
5. **No container log output.** `podman logs flexagg-jupyter` returns nothing; nginx's
   access log is the only visibility into notebook traffic.
6. **Rotate the Jupyter token after the demo.** It is on a Cloudflare-fronted host with
   the token as the only gate, and it has been shared out of band.
7. **Deck, slide 7.** The FHS panel says the method "lands at 1.0%" while the Kupiec
   panel beside it reports 30 exceptions on 2,368 days = 1.27%. Reconcile or qualify.


---

## Outcome — 2026-08-21, both notebooks now green

**14 passed, 0 failed.** Soros and Vanguard both execute top-to-bottom with zero error outputs and
execution counts `1..19`.

Three things the plan got wrong, all found by running it:

1. **Step 1's command could never work.** `PYTHONPATH=python_src` is relative, and nbconvert runs
   the kernel with cwd `notebooks/`, so it resolved to `notebooks/python_src` and every run died on
   `ModuleNotFoundError: No module named 'notebook_helpers'`. Step 2 already used the absolute path;
   Step 1 now does too. Fixed above.

2. **"No code change is needed" was wrong.** Both notebooks' COVID cells (31 and 33) built their
   DataFrames off the Day/DayDate query, derived a `date` column, and left the raw `DayDate` column
   — real `datetime.date` objects — in the frame. Altair serialises EVERY column of the frame it is
   handed, not just the encoded ones, and `datetime.date` is not JSON-serialisable, so both chart
   cells died with `TypeError: Object of type date is not JSON serializable`. That is why the
   execution counts were ragged with `None`s: the last person to run it hit this and stopped. Fixed
   by coercing `DayDate` itself, one line per cell, in both notebooks.

3. **Vanguard was pinned to a stale date.** `D = dt.date(2024, 12, 31)`, with the comment "latest
   monthly COB in the sample" — 18 months behind the build's actual latest COB of 2026-06-30. Every
   figure in that notebook was as-of Dec 2024 and looked current. Re-pointed and re-executed. This
   is the same stale-constant fault `test_notebook.py`'s own `_notebook_D` docstring records having
   had; it had simply moved into the notebook.

**Step 3 is done, not deferred.** `test_notebook.py` is parameterised over both notebooks (each
reads its own `D` from its own source) and gained `t_day_frames_coerce_daydate`, a regression test
for defect 2 above. Vanguard is no longer unguarded, so the "unguarded notebook is a liability"
caveat no longer applies to it.

**Step 4 (commit) has NOT been done.** Working tree carries: both notebooks re-executed,
`python_src/test_notebook.py` rewritten, and this file. Backups in `/tmp`:
`soros_13f_risk.ipynb.bak-preheadless-1787342816` and `vanguard_13f_risk.ipynb.bak-*`.
