"""Container liveness check: is the heartbeat file fresh? Exit 0 (healthy) / 1 (stale or missing).

The running bot's ``_heartbeat`` loop rewrites ``HEARTBEAT_PATH`` every minute; the Dockerfile
``HEALTHCHECK`` runs ``python -m roger.health`` to read it. A wedged event loop or a hung process
stops refreshing the file, so the mtime goes stale and the container flips to *unhealthy* — the
point being that "the container is running" and "the bot is working" are different claims.

Kept import-free (stdlib only) so the check is cheap enough to run every minute and unit-testable.
"""

from __future__ import annotations

import os
import sys
import time

HEARTBEAT_PATH = "/tmp/roger.healthy"  # noqa: S108 - tmpfs mount, the only writable ephemeral path
MAX_AGE_S = 180  # three missed 60s beats -> unhealthy


def is_fresh(path: str, max_age_s: float, *, now: float | None = None) -> bool:
    """True if ``path`` exists and was written within ``max_age_s`` seconds (pure, testable)."""
    reference = time.time() if now is None else now
    try:
        return reference - os.path.getmtime(path) < max_age_s
    except OSError:  # missing file (or unreadable) — treat as not alive
        return False


def main() -> int:
    return 0 if is_fresh(HEARTBEAT_PATH, MAX_AGE_S) else 1


if __name__ == "__main__":
    sys.exit(main())
