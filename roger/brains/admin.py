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

MAX_TOOL_CALLS = 5  # hard budget per request (§2.9)
MAX_TURNS = 8  # safety bound on model round-trips

Confirmer = Callable[[str], Awaitable[bool]]

SYSTEM_PROMPT = (
    "You are Roger, a Discord server admin assistant. You may only act through the provided "
    "tools. If a request falls outside them, say so plainly — do not pretend. Keep replies short "
    "and factual; no personality flourishes. The current server state is provided as JSON."
)


async def _deny_all(_: str) -> bool:
    return False


async def handle_admin_request(
    *,
    request: str,
    guild: Any,
    actor_id: int,
    llm: LLM,
    store: Store,
    confirm: Confirmer | None = None,
    ctx: ToolContext | None = None,
) -> str:
    confirm = confirm or _deny_all

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
        {"role": "user", "content": request},
    ]

    tool_calls_used = 0
    turns = 0
    try:
        while True:
            turns += 1
            if turns > MAX_TURNS:
                return "I couldn't finish that within my step budget."

            response = await llm.complete("admin", messages, tools=tools)
            message = response.choices[0].message

            if not getattr(message, "tool_calls", None):
                return message.content or "(no response)"

            messages.append(_assistant_message(message))
            for call in message.tool_calls:
                if tool_calls_used >= MAX_TOOL_CALLS:
                    await store.record_audit(
                        actor_id=actor_id,
                        brain="admin",
                        tool=call.function.name,
                        args=None,
                        status=AuditStatus.DENIED,
                        detail="tool budget",
                    )
                    messages.append(
                        _tool_message(call.id, {"error": "tool-call budget (5) exhausted"})
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
    except BudgetExceeded:
        await store.record_audit(
            actor_id=actor_id,
            brain="admin",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail="daily token cap",
        )
        return "I've hit my daily token budget for admin work. Try again tomorrow."
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
        if spec.requires_confirm:
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
