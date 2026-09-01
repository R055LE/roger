"""Admin tool loop — driven with a scripted fake LLM and a real temp store (no network)."""

import asyncio
from types import SimpleNamespace

import pytest

from roger.brains import admin
from roger.llm import BudgetExceeded
from roger.request_context import current_request_id, request_context
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


async def test_audit_rows_for_one_multitool_request_share_the_bound_id(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "list_structure")]),
                _resp(tool_calls=[_tool_call("c2", "server_stats")]),
                _resp(content="Done."),
            ]
        )
        with request_context() as request_id:
            assert await admin.handle_admin_request(
                request="inspect", guild=object(), actor_id=1, llm=llm, store=store
            ) == "Done."
        assert {row["request_id"] for row in await store.fetch_audit()} == {request_id}
    finally:
        await store.close()


async def test_request_context_isolated_between_concurrent_admin_requests(tmp_path):
    store = await _open_store(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str | None] = []

    class BlockingLLM:
        async def complete(self, brain, messages, tools=None):
            seen.append(current_request_id())
            started.set()
            await release.wait()
            seen.append(current_request_id())
            return _resp(content="Done.")

    async def run(request):
        with request_context() as request_id:
            answer = await admin.handle_admin_request(
                request=request, guild=object(), actor_id=1, llm=BlockingLLM(), store=store
            )
            return request_id, answer

    try:
        first = asyncio.create_task(run("first"))
        await started.wait()
        second = asyncio.create_task(run("second"))
        await asyncio.sleep(0)
        release.set()
        request_ids = {request_id for request_id, answer in await asyncio.gather(first, second)}
        assert len(request_ids) == 2
        assert set(seen) == request_ids
        assert {row["request_id"] for row in await store.fetch_audit()} == request_ids
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


async def test_tool_call_budget_caps_at_default(tmp_path):
    store = await _open_store(tmp_path)
    try:
        cap = admin.MAX_TOOL_CALLS
        calls = [_tool_call(f"c{i}", "list_structure") for i in range(cap + 1)]
        llm = FakeLLM([_resp(tool_calls=calls), _resp(content="done")])
        notified = []

        async def fake_notify(message):
            notified.append(message)

        out = await admin.handle_admin_request(
            request="spam tools",
            guild=object(),
            actor_id=1,
            llm=llm,
            store=store,
            notify_ops=fake_notify,
        )
        assert out == "done"
        rows = await store.fetch_audit()
        ok = [r for r in rows if r["tool"] == "list_structure" and r["status"] == "ok"]
        denied = [r for r in rows if r["status"] == "denied" and r["detail"] == "tool budget"]
        assert len(ok) == cap
        assert len(denied) == 1
        assert len(notified) == 1
        assert "tool-call budget" in notified[0]
    finally:
        await store.close()


_SET_PERMS_ARGS = (
    '{"channel": "general", "overwrites": '
    '[{"target": "@everyone", "deny": ["send_messages"]}]}'
)


def _patch_confirm_tool(monkeypatch, applied):
    async def fake_preview(name, guild, args, ctx=None):
        return "DIFF"

    async def fake_set_perms(guild, args, ctx=None):
        applied["done"] = True
        return {"channel": "general", "applied": []}

    async def fake_audit(guild, args, ctx=None):
        return {"status": "ok", "destinations": [], "remediation": []}

    monkeypatch.setattr(admin.executors, "preview", fake_preview)
    monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", fake_set_perms)
    monkeypatch.setitem(admin.executors.EXECUTORS, "audit_permissions", fake_audit)


async def test_set_permissions_executes_when_approved(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        applied = {}
        _patch_confirm_tool(monkeypatch, applied)

        async def approve(diff):
            return True

        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
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
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
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
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
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


async def test_set_permissions_requires_successful_audit_before_preview(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        seen = {"preview": 0, "confirm": 0, "execute": 0}

        async def preview(*args):
            seen["preview"] += 1
            return "DIFF"

        async def execute(*args):
            seen["execute"] += 1
            return {}

        async def confirm(diff):
            seen["confirm"] += 1
            return True

        monkeypatch.setattr(admin.executors, "preview", preview)
        monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", execute)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("c1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="Audit first."),
            ]
        )
        assert await admin.handle_admin_request(
            request="lock", guild=object(), actor_id=1, llm=llm, store=store, confirm=confirm
        ) == "Audit first."
        assert seen == {"preview": 0, "confirm": 0, "execute": 0}
        rows = await store.fetch_audit()
        assert any(r["detail"] == "permission audit required" for r in rows)
    finally:
        await store.close()


async def test_no_unique_role_audit_keeps_permission_mutation_blocked(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        seen = {"preview": 0, "confirm": 0, "execute": 0}

        async def audit(*args):
            return {
                "status": "no unique dedicated bot role",
                "destinations": [],
                "remediation": [],
            }

        async def preview(*args):
            seen["preview"] += 1
            return "DIFF"

        async def execute(*args):
            seen["execute"] += 1
            return {}

        async def confirm(diff):
            seen["confirm"] += 1
            return True

        monkeypatch.setitem(admin.executors.EXECUTORS, "audit_permissions", audit)
        monkeypatch.setattr(admin.executors, "preview", preview)
        monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", execute)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
                _resp(tool_calls=[_tool_call("c1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="The permission change is blocked."),
            ]
        )

        assert await admin.handle_admin_request(
            request="lock", guild=object(), actor_id=1, llm=llm, store=store, confirm=confirm
        ) == "The permission change is blocked."
        assert seen == {"preview": 0, "confirm": 0, "execute": 0}
        rows = await store.fetch_audit()
        assert any(
            row["tool"] == "set_permissions"
            and row["status"] == "invalid"
            and row["detail"] == "permission audit required"
            for row in rows
        )
    finally:
        await store.close()


def _missing_permission_audit_result():
    return {
        "status": "ok",
        "dedicated_role": {"id": 12, "name": "Roger integration"},
        "destinations": [
            {
                "destination": "digest",
                "channel": "daily-digest",
                "missing": ["embed_links"],
                "category_permission_sync": "not_synced",
                "missing_causes": {
                    "embed_links": "channel @everyone overwrite",
                },
            }
        ],
        "remediation": [
            {
                "destination": "digest",
                "scope": "channel",
                "target": "daily-digest",
                "role": "Roger integration",
            }
        ],
    }


async def test_permission_audit_evidence_replaces_speculative_model_text(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        async def audit(*args):
            return _missing_permission_audit_result()

        monkeypatch.setitem(admin.executors.EXECUTORS, "audit_permissions", audit)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
                _resp(content="The digest feeds must be missing."),
            ]
        )

        answer = await admin.handle_admin_request(
            request="why is the digest empty?", guild=object(), actor_id=1, llm=llm, store=store
        )

        assert "feeds" not in answer
        assert "digest destination (#daily-digest)" in answer
        assert "embed_links (channel @everyone overwrite)" in answer
        assert "category sync: not_synced" in answer
        assert "role Roger integration on channel daily-digest" in answer
    finally:
        await store.close()


async def test_applied_permissions_replace_stale_audit_response(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        async def audit(*args):
            return _missing_permission_audit_result()

        async def preview(*args):
            return "DIFF"

        async def execute(*args):
            return {
                "channel": "daily-digest",
                "applied": [
                    {"target": "@everyone", "allow": [], "deny": ["embed_links"]},
                    {
                        "target": "Roger integration",
                        "allow": ["view_channel", "send_messages", "embed_links"],
                        "deny": [],
                    },
                ],
            }

        async def approve(diff):
            return True

        monkeypatch.setitem(admin.executors.EXECUTORS, "audit_permissions", audit)
        monkeypatch.setattr(admin.executors, "preview", preview)
        monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", execute)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
                _resp(tool_calls=[_tool_call("p1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="The missing feeds are repaired and reachable."),
            ]
        )

        answer = await admin.handle_admin_request(
            request="fix digest access",
            guild=object(),
            actor_id=1,
            llm=llm,
            store=store,
            confirm=approve,
        )

        assert answer == (
            "Permission replacements applied on #daily-digest: "
            "@everyone: allow[—] deny[embed_links]; "
            "Roger integration: allow[view_channel, send_messages, embed_links] deny[—]. "
            "A follow-up permission audit verifies effective access."
        )
        assert "missing" not in answer
        assert "feeds" not in answer and "repaired" not in answer and "reachable" not in answer
    finally:
        await store.close()


async def test_denied_permissions_preserve_current_audit_response(tmp_path, monkeypatch):
    store = await _open_store(tmp_path)
    try:
        executed = False

        async def audit(*args):
            return _missing_permission_audit_result()

        async def preview(*args):
            return "DIFF"

        async def execute(*args):
            nonlocal executed
            executed = True
            return {"channel": "daily-digest", "applied": []}

        async def deny(diff):
            return False

        monkeypatch.setitem(admin.executors.EXECUTORS, "audit_permissions", audit)
        monkeypatch.setattr(admin.executors, "preview", preview)
        monkeypatch.setitem(admin.executors.EXECUTORS, "set_permissions", execute)
        llm = FakeLLM(
            [
                _resp(tool_calls=[_tool_call("a1", "audit_permissions")]),
                _resp(tool_calls=[_tool_call("p1", "set_permissions", _SET_PERMS_ARGS)]),
                _resp(content="Everything was applied."),
            ]
        )

        answer = await admin.handle_admin_request(
            request="fix digest access",
            guild=object(),
            actor_id=1,
            llm=llm,
            store=store,
            confirm=deny,
        )

        assert executed is False
        assert "missing embed_links (channel @everyone overwrite)" in answer
        assert "category sync: not_synced" in answer
        assert "applied" not in answer
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


async def test_budget_exceeded_audit_detail_reflects_unit(tmp_path):
    store = await _open_store(tmp_path)
    try:
        llm = FakeLLM([BudgetExceeded("admin", 2.5, 2.0, unit="usd")])
        await admin.handle_admin_request(
            request="anything", guild=object(), actor_id=1, llm=llm, store=store
        )
        rows = await store.fetch_audit()
        assert any(r["detail"] == "daily $ cap" for r in rows)
    finally:
        await store.close()
