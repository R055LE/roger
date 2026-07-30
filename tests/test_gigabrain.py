"""Gigabrain tool loop — read-only by construction, driven with a scripted fake LLM."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from roger.brains import gigabrain
from roger.llm import BudgetExceeded
from roger.store import Store
from roger.tools import schemas


def _resp(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


class FakeLLM:
    """Returns (or raises) the next scripted item on each complete() call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def complete(self, brain, messages, tools=None):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _patch_snapshot(monkeypatch):
    async def fake_snapshot(guild, *, detailed=False):
        return {
            "categories": [{"id": 1, "name": "Media"}],
            "channels": [
                {
                    "id": 2,
                    "name": "general",
                    "kind": "text",
                    "category": None,
                    "topic": None,
                    "overwrites": {},
                }
            ],
            "roles": [{"id": 3, "name": "@everyone", "position": 0, "color": "#000000"}],
        }

    monkeypatch.setattr(gigabrain.executors, "snapshot", fake_snapshot)


async def _open_store(tmp_path):
    return await Store(str(tmp_path / "gigabrain.db")).open()


def test_gigabrain_tools_are_all_read_only_and_confirm_free():
    """Every allowlisted tool exists and never requires confirmation — the no-mutation invariant."""
    for name in gigabrain.GIGABRAIN_TOOLS:
        spec = schemas.REGISTRY[name]
        assert spec.confirm_when is None
        assert spec.requires_confirm is False


async def test_text_only_answer_records_request_row(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([_resp(content="This server looks fine.")])
        out = await gigabrain.handle_gigabrain_request(
            request="how are we doing?", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "This server looks fine."
        rows = await store.fetch_audit()
        assert any(r["brain"] == "gigabrain" and r["detail"] == "request" for r in rows)
    finally:
        await store.close()


async def test_tool_call_then_answer(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "list_structure")]),
                _resp(content="Media has #general."),
            ]
        )
        out = await gigabrain.handle_gigabrain_request(
            request="what's here?", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert "general" in out
        rows = await store.fetch_audit()
        assert any(
            r["tool"] == "list_structure" and r["status"] == "ok" and r["brain"] == "gigabrain"
            for r in rows
        )
    finally:
        await store.close()


async def test_budget_exceeded_returns_polite_refusal(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("gigabrain", 100, 50)])
        out = await gigabrain.handle_gigabrain_request(
            request="anything", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert "budget" in out.lower()
    finally:
        await store.close()


async def test_unknown_tool_is_structured_error(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "delete_everything")]),
                _resp(content="I can't do that."),
            ]
        )
        out = await gigabrain.handle_gigabrain_request(
            request="nuke it", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "I can't do that."
        rows = await store.fetch_audit()
        assert any(r["tool"] == "delete_everything" and r["status"] == "invalid" for r in rows)
    finally:
        await store.close()


async def test_mutating_tool_outside_the_allowlist_is_refused_without_executing(
    tmp_path, monkeypatch
):
    """A real, existing tool name is refused if it isn't in GIGABRAIN_TOOLS — not just unoffered."""
    store = await _open_store(tmp_path)
    try:
        called = {}

        async def spy_delete_role(guild, args, ctx=None):
            called["ran"] = True
            return {"status": "deleted"}

        monkeypatch.setitem(gigabrain.executors.EXECUTORS, "delete_role", spy_delete_role)

        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "delete_role", '{"role": "spam"}')]),
                _resp(content="Refused."),
            ]
        )
        out = await gigabrain.handle_gigabrain_request(
            request="delete a role", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "Refused."
        assert "ran" not in called  # the executor must never have been reached
        rows = await store.fetch_audit()
        assert any(r["tool"] == "delete_role" and r["detail"] == "not allowlisted" for r in rows)
    finally:
        await store.close()


async def test_confirm_gated_allowlisted_tool_is_refused_not_executed(tmp_path, monkeypatch):
    """Defense in depth: if the allowlist ever drifts to include a confirm-gated tool, refuse it."""
    store = await _open_store(tmp_path)
    try:
        original_spec = schemas.REGISTRY["server_stats"]
        monkeypatch.setitem(
            schemas.REGISTRY, "server_stats", replace(original_spec, requires_confirm=True)
        )
        called = {}

        async def spy_server_stats(guild, args, ctx=None):
            called["ran"] = True
            return {}

        monkeypatch.setitem(gigabrain.executors.EXECUTORS, "server_stats", spy_server_stats)

        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "server_stats")]),
                _resp(content="Skipped."),
            ]
        )
        out = await gigabrain.handle_gigabrain_request(
            request="stats", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "Skipped."
        assert "ran" not in called
    finally:
        await store.close()


async def test_tool_call_budget_caps_at_default(tmp_path):
    store = await _open_store(tmp_path)
    try:
        cap = gigabrain.MAX_TOOL_CALLS
        calls = [_tool_call(f"c{i}", "list_structure") for i in range(cap + 1)]
        llm = FakeLLM([_resp(tool_calls=calls), _resp(content="done")])
        out = await gigabrain.handle_gigabrain_request(
            request="spam tools", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "done"
        rows = await store.fetch_audit()
        ok = [r for r in rows if r["tool"] == "list_structure" and r["status"] == "ok"]
        denied = [r for r in rows if r["status"] == "denied" and r["detail"] == "tool budget"]
        assert len(ok) == cap
        assert len(denied) == 1
    finally:
        await store.close()


class _CapturingLLM(FakeLLM):
    """FakeLLM that records the messages it was last called with."""

    def __init__(self, script, sink):
        super().__init__(script)
        self._sink = sink

    async def complete(self, brain, messages, tools=None):
        self._sink["messages"] = messages
        return await super().complete(brain, messages, tools)


async def test_gigabrain_memory_persists_and_reloads(tmp_path):
    store = await _open_store(tmp_path)
    try:
        out1 = await gigabrain.handle_gigabrain_request(
            request="what should I improve?",
            guild=object(),
            actor_id=1,
            llm=FakeLLM([_resp(content="Consider a rules channel.")]),
            store=store,
            channel_id=42,
        )
        assert out1 == "Consider a rules channel."

        sink = {}
        out2 = await gigabrain.handle_gigabrain_request(
            request="say more about that",
            guild=object(),
            actor_id=1,
            llm=_CapturingLLM([_resp(content="Sure.")], sink),
            store=store,
            channel_id=42,
        )
        assert out2 == "Sure."
        contents = [m["content"] for m in sink["messages"] if m.get("content")]
        assert any("what should I improve?" in c for c in contents)
        assert any("Consider a rules channel." in c for c in contents)
    finally:
        await store.close()


async def test_gigabrain_memory_is_scoped_per_channel(tmp_path):
    store = await _open_store(tmp_path)
    try:
        await gigabrain.handle_gigabrain_request(
            request="channel A request",
            guild=object(),
            actor_id=1,
            llm=FakeLLM([_resp(content="A done.")]),
            store=store,
            channel_id=1,
        )
        sink = {}
        await gigabrain.handle_gigabrain_request(
            request="channel B request",
            guild=object(),
            actor_id=1,
            llm=_CapturingLLM([_resp(content="B done.")], sink),
            store=store,
            channel_id=2,
        )
        contents = [m["content"] for m in sink["messages"] if m.get("content")]
        assert not any("channel A request" in c for c in contents)
    finally:
        await store.close()
