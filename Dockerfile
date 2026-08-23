# Pinned by digest (not just the moving 3.14-slim tag) so builds are reproducible; Dependabot bumps
# the digest + comment on a new base release. Tag retained for readability.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

# Non-root runtime user with a fixed uid/gid so the host can chown the bind-mounted /data
# to match. The app writes only to /data (volume) and /tmp (tmpfs), so the rootfs is read-only.
RUN groupadd --system --gid 10001 roger \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home roger \
 && apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package (deps are pinned in pyproject.toml).
COPY pyproject.toml README.md ./
COPY roger ./roger
RUN pip install --no-cache-dir . \
 && pip uninstall --yes pip setuptools

RUN mkdir -p /data && chown roger:roger /data

USER roger
ENV PYTHONUNBUFFERED=1

# Build identity, surfaced in the boot self-report. Declared last so a new commit SHA only rebuilds
# this trivial layer, not the dependency install above. Defaults to "dev" for local builds.
ARG ROGER_VERSION=dev
ENV ROGER_VERSION=${ROGER_VERSION}

# Liveness: the bot's heartbeat loop refreshes /tmp/roger.healthy every 60s; a wedged event loop
# lets it go stale and the container flips to unhealthy. start-period covers the gateway connect.
HEALTHCHECK --interval=60s --timeout=5s --start-period=45s --retries=3 \
  CMD ["python", "-m", "roger.health"]

CMD ["python", "-m", "roger"]
