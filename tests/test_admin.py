"""Admin tool loop — driven with a scripted fake LLM and a real temp store (no network)."""

from types import SimpleNamespace

import pytest

from roger.brains import admin
from roger.llm import BudgetExceeded
from roger.store import Store


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
    async def fake_snapshot(guild):
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

    monkeypatch.setattr(admin.executors, "snapshot", fake_snapshot)


async def _open_store(tmp_path):
    return await Store(str(tmp_path / "admin.db")).open()


async def test_text_only_answer_records_request_row(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([_resp(content="We have #general.")])
        out = await admin.handle_admin_request(
            request="what channels?", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "We have #general."
        rows = await store.fetch_audit()
        assert any(r["detail"] == "request" for r in rows)
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
        out = await admin.handle_admin_request(
            request="list", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert "general" in out
        rows = await store.fetch_audit()
        assert any(r["tool"] == "list_structure" and r["status"] == "ok" for r in rows)
    finally:
        await store.close()


async def test_budget_exceeded_returns_polite_refusal(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("admin", 100, 50)])
        out = await admin.handle_admin_request(
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
        out = await admin.handle_admin_request(
            request="nuke it", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "I can't do that."
        rows = await store.fetch_audit()
        assert any(r["tool"] == "delete_everything" and r["status"] == "invalid" for r in rows)
    finally:
        await store.close()


async def test_tool_call_budget_caps_at_five(tmp_path):
    store = await _open_store(tmp_path)
    try:
        six_calls = [_tool_call(f"c{i}", "list_structure") for i in range(6)]
        llm = FakeLLM([_resp(tool_calls=six_calls), _resp(content="done")])
        out = await admin.handle_admin_request(
            request="spam tools", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "done"
        rows = await store.fetch_audit()
        ok = [r for r in rows if r["tool"] == "list_structure" and r["status"] == "ok"]
        denied = [r for r in rows if r["status"] == "denied" and r["detail"] == "tool budget"]
        assert len(ok) == 5
        assert len(denied) == 1
    finally:
        await store.close()
