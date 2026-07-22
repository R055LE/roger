FROM python:3.12-slim

# Non-root runtime user. The app writes only to /data (volume) and /tmp (tmpfs), so the
# rootfs can be mounted read-only.
RUN groupadd --system roger \
 && useradd --system --gid roger --home-dir /app --no-create-home roger \
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
CMD ["python", "-m", "roger"]
