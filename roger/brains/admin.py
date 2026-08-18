"""Admin brain: the hand-rolled tool loop (§6).

Owner-only (the gate runs at dispatch, before we ever get here). Snapshots live guild state, hands
it to the model with the tool schemas, then runs a bounded loop: validate args → guard → execute
(or pause for confirmation) → feed the result back.

Confirmation is injected as a callback so the loop stays testable and decoupled from Discord: the
bot passes a real button-driven confirmer; tests pass a fake.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.store import AuditStatus, Store
from roger.tools import executors, schemas
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError

log = logging.getLogger("roger.admin")

MAX_TOOL_CALLS = 10  # fallback hard budget per request when no settings are given (§2.9)
MAX_TURNS = 14  # fallback safety bound on model round-trips when no settings are given

Confirmer = Callable[[str], Awaitable[bool]]
NotifyOps = Callable[[str], Awaitable[None]]


async def _notify_nothing(_: str) -> None:
    return None


SYSTEM_PROMPT = (
    "You are Roger, a Discord server admin assistant. You may only act through the provided "
    "tools. If a request falls outside them, say so plainly — do not pretend. Keep replies short "
    "and factual; no personality flourishes. The current server state is provided as JSON."
)


async def _deny_all(_: str) -> bool:
    return False


async def _remember(
    store: Store, actor_id: int, channel_id: int | None, request: str, answer: str
) -> None:
    """Persist one completed turn so the next request in this channel has continuity."""
    if channel_id is None:
        return
    await store.add_admin(actor_id, channel_id, "user", request)
    await store.add_admin(actor_id, channel_id, "bot", answer)


async def handle_admin_request(
    *,
    request: str,
    guild: Any,
    actor_id: int,
    llm: LLM,
    store: Store,
    confirm: Confirmer | None = None,
    ctx: ToolContext | None = None,
    channel_id: int | None = None,
    notify_ops: NotifyOps | None = None,
) -> str:
    confirm = confirm or _deny_all
    notify_ops = notify_ops or _notify_nothing
    settings = ctx.settings if ctx is not None else None
    max_tool_calls = getattr(settings, "admin_max_tool_calls", None) or MAX_TOOL_CALLS
    max_turns = getattr(settings, "admin_max_turns", None) or MAX_TURNS

    await store.record_audit(
        actor_id=actor_id,
        brain="admin",
        tool=None,
        args={"request": request},
        status=AuditStatus.OK,
        detail="request",
    )

    snap = await executors.snapshot(guild)
    tools = schemas.openai_tools(list(schemas.REGISTRY))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Current server state:\n" + json.dumps(snap, default=str)},
    ]
    # Short multi-turn memory for this owner+channel so follow-ups ("do that", "rename it")
    # have context. Only the request/answer text is kept — tool machinery stays transient.
    if channel_id is not None:
        for turn in await store.recent_admin(actor_id, channel_id):
            role = "assistant" if turn["role"] == "bot" else "user"
            messages.append({"role": role, "content": turn["content"]})
    messages.append({"role": "user", "content": request})

    tool_calls_used = 0
    turns = 0
    tool_budget_notified = False
    try:
        while True:
            turns += 1
            if turns > max_turns:
                return "I couldn't finish that within my step budget."

            response = await llm.complete("admin", messages, tools=tools)
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                answer = message.content or "(no response)"
                await _remember(store, actor_id, channel_id, request, answer)
                return answer

            messages.append(_assistant_message(message))
            for call in message.tool_calls:
                if tool_calls_used >= max_tool_calls:
                    await store.record_audit(
                        actor_id=actor_id,
                        brain="admin",
                        tool=call.function.name,
                        args=None,
                        status=AuditStatus.DENIED,
                        detail="tool budget",
                    )
                    if not tool_budget_notified:
                        tool_budget_notified = True
                        log.warning(
                            "admin request by %s hit the tool-call budget (%d) — "
                            "remaining steps skipped for this request",
                            actor_id,
                            max_tool_calls,
                        )
                        await notify_ops(
                            f"⚠️ **admin tool-call budget hit** — actor {actor_id} needed more "
                            f"than {max_tool_calls} tool calls for one request; remaining steps "
                            "were skipped. Raise ADMIN_MAX_TOOL_CALLS if this is routine."
                        )
                    messages.append(
                        _tool_message(
                            call.id,
                            {
                                "error": (
                                    f"tool-call limit for this request reached ({max_tool_calls} "
                                    "calls). This is a per-request cap, not a timed cooldown — "
                                    "tell the user to send a new message to continue with any "
                                    "remaining steps."
                                )
                            },
                        )
                    )
                    continue

                tool_calls_used += 1
                result, status, detail = await _run_tool(call, guild, confirm, ctx)
                await store.record_audit(
                    actor_id=actor_id,
                    brain="admin",
                    tool=call.function.name,
                    args=_safe_args(call),
                    status=status,
                    detail=detail,
                )
                messages.append(_tool_message(call.id, result))
    except BudgetExceeded as exc:
        await store.record_audit(
            actor_id=actor_id,
            brain="admin",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail=f"daily {'$' if exc.unit == 'usd' else 'token'} cap",
        )
        return "I've hit my daily budget for admin work. Try again tomorrow."
    except LLMConfigError as exc:
        return f"The admin brain isn't configured yet ({exc})."


async def _run_tool(
    call: Any, guild: Any, confirm: Confirmer, ctx: ToolContext | None
) -> tuple[dict[str, Any], AuditStatus, str | None]:
    spec = schemas.REGISTRY.get(call.function.name)
    if spec is None:
        return {"error": f"unknown tool: {call.function.name}"}, AuditStatus.INVALID, "unknown tool"

    try:
        raw = json.loads(call.function.arguments or "{}")
        args = spec.args_model.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        return {"error": f"invalid arguments: {exc}"}, AuditStatus.INVALID, "arg validation"

    try:
        if spec.needs_confirm(args):
            diff = await executors.preview(spec.name, guild, args)
            if not await confirm(diff):
                return {"status": "denied by owner"}, AuditStatus.DENIED, "owner denied"
        result = await executors.EXECUTORS[spec.name](guild, args, ctx)
        return result, AuditStatus.OK, None
    except GuardError as exc:
        return {"error": str(exc)}, AuditStatus.INVALID, "guard"
    except Exception as exc:  # surfaced to the model as a structured result, not raised
        log.exception("executor for %s failed", spec.name)
        return {"error": str(exc)}, AuditStatus.ERROR, "executor error"


def _assistant_message(message: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ],
    }


def _tool_message(tool_call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(result, default=str),
    }


def _safe_args(call: Any) -> dict[str, Any]:
    try:
        return json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {"_raw": call.function.arguments}
