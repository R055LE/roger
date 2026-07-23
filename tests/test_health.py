"""Container liveness — heartbeat freshness check + the bot's heartbeat writer (backlog 1.4)."""

import os
import time

from roger.bot import _touch_heartbeat
from roger.health import is_fresh


def test_is_fresh_true_for_a_recent_file(tmp_path):
    path = tmp_path / "hb"
    path.write_text("x")
    assert is_fresh(str(path), 180) is True


def test_is_fresh_false_for_a_stale_file(tmp_path):
    path = tmp_path / "hb"
    path.write_text("x")
    old = time.time() - 1000
    os.utime(str(path), (old, old))
    assert is_fresh(str(path), 180) is False


def test_is_fresh_false_when_missing(tmp_path):
    assert is_fresh(str(tmp_path / "nope"), 180) is False


def test_touch_heartbeat_writes_a_fresh_file(tmp_path):
    path = str(tmp_path / "hb")
    _touch_heartbeat(path)
    assert is_fresh(path, 180) is True
