"""Jupyter Server config for the hardened flexagg-jupyter container.

Mounted read-only at /app/jupyter_config, found via JUPYTER_CONFIG_PATH (see
docker/flexagg-jupyter.container). Kept OUT of the image so it can change without a rebuild.

Why this exists: JupyterLab does not stop a kernel when its browser tab closes — the kernel and,
here, the whole Atoti JVM it started keep running until the container restarts. Closing a tab and
reopening the notebook therefore reattaches to a warm kernel whose cube already holds :9096, and
every abandoned session keeps its multi-GB heap inside a container capped at 12g. On 2026-08-21
two abandoned kernels were holding 5.4 GB between them while serving nobody.

Culling reaps them. `cull_connected = False` is the right setting for that failure: a closed tab
leaves the kernel DISCONNECTED, so it is culled on idle, while a kernel someone still has open
stays up no matter how long they read the output.
"""
c = get_config()  # noqa: F821  (injected by traitlets)

# ── ipywidgets frontend, without an image rebuild ───────────────────────────────────────────
# The demo notebook's manager dropdown (notebook_helpers.manager_picker) needs ipywidgets. Its
# PYTHON half rides the existing data/_pylibs staging on PYTHONPATH like altair/narwhals, but its
# FRONTEND is a JupyterLab labextension, which is discovered from jupyter data dirs and so is
# invisible to PYTHONPATH — stage only the python half and the dropdown renders as a dead text
# repr. Declaring the staged labextensions dir here is enough, and keeps the change in this
# already-mounted file rather than in the Quadlet unit or the image.
#   Restage after a version bump:
#     cp -r barra/lib/python3.12/site-packages/{ipywidgets,jupyterlab_widgets,widgetsnbextension} data/_pylibs/
#     cp -r barra/share/jupyter/labextensions data/_pylibs/share/jupyter/
# Verified on the host by serving /lab with the venv's own copy removed: the extension is served
# from this path alone. Takes effect on a container restart (server-side discovery), not a rebuild.
c.LabApp.extra_labextensions_path = ["/app/data/_pylibs/share/jupyter/labextensions"]

c.MappingKernelManager.cull_idle_timeout = 1800   # 30 min idle -> stop the kernel (and its JVM)
c.MappingKernelManager.cull_interval = 120        # check every 2 min
c.MappingKernelManager.cull_connected = False     # never cull a kernel with a live browser
c.MappingKernelManager.cull_busy = False          # never cull mid-execution: a build is not idle

# jupyter_server 2.x instantiates the async subclass; set both so the values apply either way.
c.AsyncMappingKernelManager.cull_idle_timeout = c.MappingKernelManager.cull_idle_timeout
c.AsyncMappingKernelManager.cull_interval = c.MappingKernelManager.cull_interval
c.AsyncMappingKernelManager.cull_connected = c.MappingKernelManager.cull_connected
c.AsyncMappingKernelManager.cull_busy = c.MappingKernelManager.cull_busy
