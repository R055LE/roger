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


_SET_PERMS_ARGS = (
    '{"channel": "general", "overwrites": '
    '[{"target": "@everyone", "deny": ["send_messages"]}]}'
)


def _patch_confirm_tool(monkeypatch, applied):
    async def fake_preview(name, guild, args):
        return "DIFF"

    async def fake_set_perms(guild, args, ctx=None):
        applied["done"] = True
        return {"channel": "general", "applied": []}

    monkeypatch.setattr(admin.executors, "preview", fake_preview)
    monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", fake_set_perms)


async def test_set_permissions_executes_when_approved(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        applied = {}
        _patch_confirm_tool(monkeypatch, applied)

        async def approve(diff):
            return True

        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="Done."),
            ]
        )
        out = await admin.handle_admin_request(
            request="lock #general",
            guild=object(),
            actor_id=1,
            llm=llm,
            store=store,
            confirm=approve,
        )
        assert out == "Done."
        assert applied.get("done") is True
        rows = await store.fetch_audit()
        assert any(r["tool"] == "set_permissions" and r["status"] == "ok" for r in rows)
    finally:
        await store.close()


async def test_set_permissions_skipped_when_denied(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        applied = {}
        _patch_confirm_tool(monkeypatch, applied)

        async def deny(diff):
            return False

        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="Left it alone."),
            ]
        )
        out = await admin.handle_admin_request(
            request="lock #general", guild=object(), actor_id=1, llm=llm, store=store, confirm=deny
        )
        assert out == "Left it alone."
        assert "done" not in applied  # executor never ran
        rows = await store.fetch_audit()
        assert any(r["tool"] == "set_permissions" and r["status"] == "denied" for r in rows)
    finally:
        await store.close()


async def test_set_permissions_defaults_to_deny_without_confirmer(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        applied = {}
        _patch_confirm_tool(monkeypatch, applied)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="Couldn't confirm."),
            ]
        )
        # No confirm= passed -> _deny_all -> executor must not run.
        out = await admin.handle_admin_request(
            request="lock #general", guild=object(), actor_id=1, llm=llm, store=store
        )
        assert out == "Couldn't confirm."
        assert "done" not in applied
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


async def test_admin_memory_persists_and_reloads(tmp_path):
    store = await _open_store(tmp_path)
    try:
        out1 = await admin.handle_admin_request(
            request="make a media channel",
            guild=object(),
            actor_id=1,
            llm=FakeLLM([_resp(content="Created #media.")]),
            store=store,
            channel_id=42,
        )
        assert out1 == "Created #media."

        sink = {}
        out2 = await admin.handle_admin_request(
            request="rename it",
            guild=object(),
            actor_id=1,
            llm=_CapturingLLM([_resp(content="Done.")], sink),
            store=store,
            channel_id=42,
        )
        assert out2 == "Done."
        contents = [m["content"] for m in sink["messages"] if m.get("content")]
        assert any("make a media channel" in c for c in contents)  # prior request in history
        assert any("Created #media." in c for c in contents)  # prior answer in history
    finally:
        await store.close()


async def test_admin_memory_is_scoped_per_channel(tmp_path):
    store = await _open_store(tmp_path)
    try:
        await admin.handle_admin_request(
            request="channel A request",
            guild=object(),
            actor_id=1,
            llm=FakeLLM([_resp(content="A done.")]),
            store=store,
            channel_id=1,
        )
        sink = {}
        await admin.handle_admin_request(
            request="channel B request",
            guild=object(),
            actor_id=1,
            llm=_CapturingLLM([_resp(content="B done.")], sink),
            store=store,
            channel_id=2,
        )
        contents = [m["content"] for m in sink["messages"] if m.get("content")]
        assert not any("channel A request" in c for c in contents)  # no cross-channel bleed
    finally:
        await store.close()
