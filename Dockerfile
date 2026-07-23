FROM python:3.12-slim

# Non-root runtime user with a fixed uid/gid so the host can chown the bind-mounted /data
# to match. The app writes only to /data (volume) and /tmp (tmpfs), so the rootfs is read-only.
RUN groupadd --system --gid 10001 roger \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home roger \
 && apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package (deps are pinned in pyproject.toml).
COPY pyproject.toml README.md ./
COPY roger ./roger
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown roger:roger /data

USER roger
ENV PYTHONUNBUFFERED=1

# Build identity, surfaced in the boot self-report. Declared last so a new commit SHA only rebuilds
# this trivial layer, not the dependency install above. Defaults to "dev" for local builds.
ARG ROGER_VERSION=dev
ENV ROGER_VERSION=${ROGER_VERSION}
CMD ["python", "-m", "roger"]
