# Backend container image.
#
# Base: UBI9-minimal. Red Hat-supported and unprivileged-by-default inside
# an air-gapped OpenShift estate, and smaller than the full UBI image.
# UBI9 has no Red Hat-packaged Python 3.13 (only 3.9/3.11/3.12 ship in
# AppStream as of this writing), so the interpreter itself comes from
# python-build-standalone rather than an RPM — the OS layer stays fully
# Red Hat-supported; only the interpreter build is a community artifact.
# See docs/adr for the full tradeoff.

# Pinned to a minor stream (9.8), not a frozen build id and not a
# floating `latest`. The stream keeps receiving Red Hat's CVE fixes
# within 9.8, so the image gets patched without a commit here, while the
# pin still keeps a rebuild reproducible to a known OS minor. Bumping to
# the next minor is a manual step — check for a newer 9.x periodically.
FROM registry.access.redhat.com/ubi9/ubi-minimal:9.8 AS base

ARG PYTHON_VERSION=3.13.15
ARG PYTHON_BUILD_STANDALONE_RELEASE=20260807
# In an air-gapped build, replace this ADD with `COPY` from a locally
# mirrored tarball (see docs/air-gap.md) — this URL is for connected builds
# only and is never reached from the final image. Verify the release tag
# still matches an existing asset before bumping PYTHON_VERSION:
# https://github.com/astral-sh/python-build-standalone/releases
ADD https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_STANDALONE_RELEASE}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz /tmp/python.tar.gz

RUN microdnf install -y --setopt=install_weak_deps=0 tar gzip shadow-utils \
    && tar -xzf /tmp/python.tar.gz -C /opt \
    && rm /tmp/python.tar.gz \
    && microdnf remove -y tar gzip \
    && microdnf clean all

ENV PATH="/opt/python/bin:${PATH}"

# --- Dependency layer: cached separately from application code ---
FROM base AS deps
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-compile -r requirements.txt

# --- Final image ---
FROM base AS runtime

# -r (system account) restricts useradd to UID < 1000 on RHEL/UBI, which
# conflicts with the explicit high UID convention used here — omitted
# rather than fought.
RUN groupadd -g 1001 app && useradd -u 1001 -g app -d /app -s /sbin/nologin app

COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY backend/app ./app
# `tools/` (seed_inventory, run_collector) rides along in the same image
# as the API rather than getting its own Containerfile — it's a thin CLI
# layer over the same `app` package with the same dependency set, so a
# second image would just duplicate this entire build for zero practical
# isolation benefit. The CronJob manifests that invoke `tools.
# run_collector` override this image's ENTRYPOINT/CMD; they don't build
# or reference a different image.
COPY tools ./tools

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health/live', timeout=2).status == 200 else 1)"

ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
