"""Shared fixtures. ``write_digest`` fakes Scout output for the scheduled brains."""

import datetime
import json

import pytest


def write_digest(tmp_path, entries, *, run_id=None, age_hours=0.0, feed="f"):
    """Write one Scout digest file containing these entries.

    Replaces the old fake feed: the brains no longer fetch, they read what Scout
    already selected.
    """
    started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=age_hours)
    run_id = run_id or started.strftime("%Y-%m-%dT%H:%M:%SZ") + "-test"
    directory = tmp_path / "digests"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run": {"run_id": run_id, "schema_version": 1, "status": "ok", "exit_code": 0,
                "started_at": started.isoformat()},
        "items": [
            {
                "native_id": e.id,
                "url": e.link,
                "title": e.title,
                "summary": e.summary,
                "published": (
                    datetime.datetime(*e.published_parsed[:6], tzinfo=datetime.UTC)
                    if e.published_parsed else started
                ).isoformat(),
                "relevance": 3,
                "matched": [{"topic": "t", "term": "x", "field": "text"}],
                "tags": [], "author": "", "source": "rss", "source_score": 0,
                "extra": {"feed": feed},
            }
            for e in entries
        ],
    }
    (directory / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_id


@pytest.fixture
def digest_writer(tmp_path):
    def _write(entries, **kwargs):
        return write_digest(tmp_path, entries, **kwargs)

    return _write
