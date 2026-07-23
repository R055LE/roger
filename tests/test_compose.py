"""Deploy-manifest guard: compose.yaml must forward exactly the Settings fields.

The container only receives the env vars named in compose.yaml's ``environment:`` block. A field
that exists in ``roger.config.Settings`` but is missing there is silently never delivered — the
exact bug that left ``OPS_CHANNEL_ID`` unwired. These tests keep the two in lockstep, in both
directions, so adding a config var without forwarding it (or forwarding one the app ignores) fails
CI immediately.
"""

import re
from pathlib import Path

from roger.config import Settings

_COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"
_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")


def _compose_env_keys() -> set[str]:
    """Env var names under the service's ``environment:`` block — a small YAML-free parser."""
    keys: set[str] = set()
    env_indent: int | None = None
    for line in _COMPOSE.read_text().splitlines():
        stripped = line.strip()
        if env_indent is None:
            if stripped == "environment:":
                env_indent = len(line) - len(line.lstrip())
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= env_indent:
            break  # dedented back out of the block
        match = _KEY.match(line)
        if match:
            keys.add(match.group(1))
    assert env_indent is not None, "no environment: block found in compose.yaml"
    return keys


def test_every_setting_is_forwarded_by_compose():
    expected = {name.upper() for name in Settings.model_fields}
    missing = expected - _compose_env_keys()
    assert not missing, (
        f"Settings fields not forwarded in compose.yaml environment: {sorted(missing)} "
        "— add each as `KEY: ${KEY:-}` (or `${KEY:?}` for a required secret)"
    )


def test_compose_forwards_nothing_the_app_ignores():
    expected = {name.upper() for name in Settings.model_fields}
    dangling = _compose_env_keys() - expected
    assert not dangling, (
        f"compose.yaml forwards env vars with no matching Settings field: {sorted(dangling)} "
        "— remove them, or add the field to roger.config.Settings"
    )
