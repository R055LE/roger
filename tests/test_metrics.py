"""Prometheus metrics — gauge refresh, render, and LLM counter wiring (backlog 3.1)."""

from types import SimpleNamespace

from prometheus_client import REGISTRY, generate_latest

from roger import metrics
from roger.config import Settings
from roger.llm import LLM
from roger.store import AuditStatus, Store

_ENV = {"DISCORD_TOKEN": "x", "OPENROUTER_API_KEY": "y", "OWNER_ID": "1", "GUILD_ID": "2"}


def _settings():
    return SimpleNamespace(
        daily_tokens_admin=150_000, daily_tokens_ambient=40_000, daily_tokens_digest=30_000
    )


async def test_refresh_populates_gauges_from_the_store(tmp_path):
    store = await Store(str(tmp_path / "m.db")).open()
    try:
        await store.add_usage("admin", 100, 50, cost_usd=0.0123)
        await store.add_feed("http://a", "A")
        await store.record_audit(
            actor_id=1, brain="admin", tool="create_channel", args=None,
            status=AuditStatus.OK, detail=None,
        )

        await metrics.refresh(store, _settings(), "sha-test")

        get = REGISTRY.get_sample_value
        assert get("roger_tokens_today", {"brain": "admin"}) == 150
        assert get("roger_cost_usd_today", {"brain": "admin"}) == 0.0123
        assert get("roger_tokens_cap", {"brain": "admin"}) == 150_000
        assert get("roger_feeds") == 1
        assert get("roger_audit_events", {"tool": "create_channel", "status": "ok"}) == 1
        assert get("roger_build_info", {"version": "sha-test"}) == 1

        body = generate_latest().decode()
        assert "roger_tokens_today" in body and "roger_llm_requests_total" in body
    finally:
        await store.close()


async def test_null_tool_audit_rows_are_labelled_none(tmp_path):
    store = await Store(str(tmp_path / "m.db")).open()
    try:
        # A gate rejection has no tool — it must still tally under a stable label, not blow up.
        await store.record_audit(
            actor_id=9, brain="admin", tool=None, args=None,
            status=AuditStatus.GATE_REJECTED, detail="non-owner",
        )
        await metrics.refresh(store, _settings(), "v")
        assert REGISTRY.get_sample_value(
            "roger_audit_events", {"tool": "none", "status": "gate_rejected"}
        ) == 1
    finally:
        await store.close()


async def test_completion_increments_the_request_counter(monkeypatch, tmp_path):
    for key, value in {**_ENV, "MODEL_ADMIN": "a/b"}.items():
        monkeypatch.setenv(key, value)
    store = await Store(str(tmp_path / "m.db")).open()
    try:
        llm = LLM(Settings(), store)

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, cost=0.0)
            )

        monkeypatch.setattr(llm._client.chat.completions, "create", fake_create)

        before = REGISTRY.get_sample_value("roger_llm_requests_total", {"brain": "admin"}) or 0.0
        await llm.complete("admin", [{"role": "user", "content": "hi"}])
        after = REGISTRY.get_sample_value("roger_llm_requests_total", {"brain": "admin"})
        assert after == before + 1
    finally:
        await store.close()
