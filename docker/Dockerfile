# Hardened image for the public /jupyter/ surface (risk.barra-poc.com/jupyter/).
#
# Design: the notebook only READS the six prebuilt parquet frames + runs Atoti on its
# embedded community licence. So this image carries the deps and NOTHING else — no repo
# secrets, no host home. python_src/, data/ and notebooks/ are bind-mounted read-only at
# runtime (see the Quadlet unit); the notebook is copied into a tmpfs workdir at start so
# the session is run-only / ephemeral and the host is never written to.
#
# Python 3.12 matches the host venv. Atoti 0.9.15 pulls jdk4py (a bundled JDK), so no
# system Java is required.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # the notebook's bare imports (notebook_helpers, barra_factor_risk_cube) resolve from here
    PYTHONPATH=/app/python_src

# Unprivileged in-container user. Under rootless Podman this maps to a host subuid that owns
# nothing — a container escape lands as a nobody, not as the flexnb service user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0555 /usr/local/bin/entrypoint.sh

USER appuser
EXPOSE 8888
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
