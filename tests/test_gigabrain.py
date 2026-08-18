"""Gigabrain tool loop — read-only by construction, driven with a scripted fake LLM."""

import datetime
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from roger.brains import gigabrain
from roger.llm import BudgetExceeded
from roger.store import Store
from roger.tools import schemas


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


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


# --------------------------------------------------------------------------- periodic suggestions


class FakeSendable:
    """A fake for anything with .id + .send() — a DM channel or a real guild text channel."""

    def __init__(self, channel_id=7001, raise_on_send=None):
        self.id = channel_id
        self.sent = []
        self._raise = raise_on_send

    async def send(self, embed=None, content=None, file=None):
        if self._raise is not None:
            raise self._raise
        self.sent.append(file if file is not None else (embed if embed is not None else content))


class FakeUser:
    def __init__(self, raise_on_create_dm=None, dm_channel=None):
        self._raise_on_create_dm = raise_on_create_dm
        self.dm_channel = dm_channel if dm_channel is not None else FakeSendable()

    async def create_dm(self):
        if self._raise_on_create_dm is not None:
            raise self._raise_on_create_dm
        return self.dm_channel


class FakeSuggestionClient:
    def __init__(self, guild, user, channel=None):
        self._guild = guild
        self._user = user
        self._channel = channel

    def get_guild(self, guild_id):
        return self._guild

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_user(self, user_id):
        return self._user


def _settings(**over):
    base = dict(gigabrain_interval_days=7, gigabrain_hour=9, tz="UTC", guild_id=9, owner_id=1)
    base.update(over)
    return SimpleNamespace(**base)


async def test_periodic_suggestion_not_configured_when_interval_is_zero(tmp_path):
    store = await _open_store(tmp_path)
    try:
        client = FakeSuggestionClient(guild=object(), user=FakeUser())
        out = await gigabrain.run_gigabrain_suggestion(
            client=client,
            settings=_settings(gigabrain_interval_days=0),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "periodic suggestions not configured"
    finally:
        await store.close()


async def test_periodic_suggestion_skips_when_not_due_yet(tmp_path):
    store = await _open_store(tmp_path)
    try:
        await store.set_meta(gigabrain.LAST_RUN_META_KEY, datetime.date.today().isoformat())
        client = FakeSuggestionClient(guild=object(), user=FakeUser())
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=FakeLLM([]), store=store
        )
        assert out["status"] == "not due yet"
    finally:
        await store.close()


async def test_periodic_suggestion_runs_and_dms_owner_on_first_call(tmp_path):
    store = await _open_store(tmp_path)
    try:
        user = FakeUser()
        client = FakeSuggestionClient(guild=object(), user=user)
        llm = FakeLLM([_resp(content="Add a rules channel.")])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=llm, store=store
        )
        assert out["status"] == "delivered"
        assert len(user.dm_channel.sent) == 1
        assert isinstance(user.dm_channel.sent[0], discord.Embed)
        assert "Add a rules channel." in user.dm_channel.sent[0].description
        assert await store.get_meta(gigabrain.LAST_RUN_META_KEY) is not None
    finally:
        await store.close()


async def test_periodic_suggestion_attaches_a_file_when_the_answer_is_long(tmp_path):
    store = await _open_store(tmp_path)
    try:
        user = FakeUser()
        client = FakeSuggestionClient(guild=object(), user=user)
        long_answer = "z" * 4200
        llm = FakeLLM([_resp(content=long_answer)])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=llm, store=store
        )
        assert out["status"] == "delivered"
        assert len(user.dm_channel.sent) == 1
        assert isinstance(user.dm_channel.sent[0], discord.File)
        assert user.dm_channel.sent[0].filename == "gigabrain-checkin.md"
        assert user.dm_channel.sent[0].fp.read().decode("utf-8") == long_answer
    finally:
        await store.close()


async def test_periodic_suggestion_posts_to_the_configured_channel_instead_of_dming(tmp_path):
    store = await _open_store(tmp_path)
    try:
        user = FakeUser()  # never touched — channel takes priority
        channel = FakeSendable(channel_id=555)
        client = FakeSuggestionClient(guild=object(), user=user, channel=channel)
        llm = FakeLLM([_resp(content="Add a rules channel.")])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client,
            settings=_settings(gigabrain_channel_id=555),
            llm=llm,
            store=store,
        )
        assert out["status"] == "delivered"
        assert len(channel.sent) == 1
        assert isinstance(channel.sent[0], discord.Embed)
        assert user.dm_channel.sent == []  # the DM path was never touched
    finally:
        await store.close()


async def test_periodic_suggestion_reports_when_the_configured_channel_is_missing(tmp_path):
    store = await _open_store(tmp_path)
    try:
        client = FakeSuggestionClient(guild=object(), user=FakeUser(), channel=None)
        llm = FakeLLM([])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client,
            settings=_settings(gigabrain_channel_id=555),
            llm=llm,
            store=store,
        )
        assert "555" in out["status"] and "not found" in out["status"]
        assert llm.calls == 0
    finally:
        await store.close()


async def test_periodic_suggestion_guild_not_visible(tmp_path):
    store = await _open_store(tmp_path)
    try:
        client = FakeSuggestionClient(guild=None, user=FakeUser())
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=FakeLLM([]), store=store
        )
        assert "not visible" in out["status"]
    finally:
        await store.close()


async def test_periodic_suggestion_delivery_failure_is_reported_and_meta_not_updated(tmp_path):
    store = await _open_store(tmp_path)
    try:
        dm_channel = FakeSendable(raise_on_send=_http_error(discord.Forbidden, 403))
        user = FakeUser(dm_channel=dm_channel)
        client = FakeSuggestionClient(guild=object(), user=user)
        llm = FakeLLM([_resp(content="Add a rules channel.")])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=llm, store=store
        )
        assert "delivery failed" in out["status"]
        assert await store.get_meta(gigabrain.LAST_RUN_META_KEY) is None
    finally:
        await store.close()


async def test_periodic_suggestion_create_dm_failure_skips_the_llm_call(tmp_path):
    store = await _open_store(tmp_path)
    try:
        user = FakeUser(raise_on_create_dm=_http_error(discord.Forbidden, 403))
        client = FakeSuggestionClient(guild=object(), user=user)
        llm = FakeLLM([])
        out = await gigabrain.run_gigabrain_suggestion(
            client=client, settings=_settings(), llm=llm, store=store
        )
        assert "DM failed" in out["status"]
        assert llm.calls == 0  # never spent a token if the DM can't even be opened
        assert await store.get_meta(gigabrain.LAST_RUN_META_KEY) is None
    finally:
        await store.close()


async def test_periodic_suggestion_remembers_the_previous_check_in(tmp_path):
    store = await _open_store(tmp_path)
    try:
        user = FakeUser()
        client = FakeSuggestionClient(guild=object(), user=user)

        out1 = await gigabrain.run_gigabrain_suggestion(
            client=client,
            settings=_settings(),
            llm=FakeLLM([_resp(content="Consider a rules channel.")]),
            store=store,
        )
        assert out1["status"] == "delivered"

        # Back-date last-run so the second call is due, then confirm the DM channel's memory
        # (not None) actually threaded through by checking what the second LLM call was shown.
        a_week_ago = datetime.date.today() - datetime.timedelta(days=7)
        await store.set_meta(gigabrain.LAST_RUN_META_KEY, a_week_ago.isoformat())
        sink = {}
        out2 = await gigabrain.run_gigabrain_suggestion(
            client=client,
            settings=_settings(),
            llm=_CapturingLLM([_resp(content="Still no rules channel.")], sink),
            store=store,
        )
        assert out2["status"] == "delivered"
        contents = [m["content"] for m in sink["messages"] if m.get("content")]
        assert any("Consider a rules channel." in c for c in contents)
    finally:
        await store.close()


async def test_budget_exceeded_audit_detail_reflects_unit(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("gigabrain", 2.5, 2.0, unit="usd")])
        await gigabrain.handle_gigabrain_request(
            request="anything", guild=object(), actor_id=1, llm=llm, store=store
        )
        rows = await store.fetch_audit()
        assert any(r["detail"] == "daily usd cap" for r in rows)
    finally:
        await store.close()
