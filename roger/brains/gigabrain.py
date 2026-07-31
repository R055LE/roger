"""Giga Brain: read-only strategic analysis, owner-only, no mutation ever.

Where admin (§6) is optimized to be cheap, fast, and act, Giga Brain is optimized to think: it looks
at live server state and reasons about it, but cannot change anything. That's enforced twice, not
just by prompting — its tool schema is built from ``GIGABRAIN_TOOLS``, a fixed allowlist of the
existing read-only tools, and ``_run_tool`` below refuses to execute anything outside that allowlist
even if a model calls a tool name it was never offered. There is deliberately no confirm callback
anywhere in this module: nothing it can reach ever needs one.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
from typing import Any
from zoneinfo import ZoneInfo

import discord
from pydantic import ValidationError

from roger.llm import LLM, BudgetExceeded, LLMConfigError
from roger.store import AuditStatus, Store
from roger.tools import executors, schemas
from roger.tools.context import ToolContext
from roger.tools.guard import GuardError

log = logging.getLogger("roger.gigabrain")

# Read-only tools only — server structure/state, not digest's content-curation domain
# (list_feeds/suggest_feeds are deliberately excluded, that's digest's own concern).
GIGABRAIN_TOOLS: tuple[str, ...] = (
    "list_structure",
    "server_stats",
    "list_role_members",
    "list_audit_log",
    "list_invites",
    "list_webhooks",
    "list_scheduled_events",
    "list_forum_posts",
)

MAX_TOOL_CALLS = 10  # fallback hard budget per request when no settings are given
MAX_TURNS = 14  # fallback safety bound on model round-trips when no settings are given

SYSTEM_PROMPT = (
    "You are Roger's strategic-analysis mode. You have read-only access to this Discord server's "
    "structure, roles, audit log, invites, webhooks, scheduled events, and forum posts, via the "
    "provided tools — you have no ability to change anything, and never will. Think carefully and "
    "give substantive, specific analysis, not generic advice. Phrase findings as observations and "
    "suggestions, never as things you did or are doing — you took no action. If a suggestion is "
    "concrete enough to act on, say the owner can ask Roger's admin mode (/roger) to do it."
)

# Periodic, unprompted check-in (§12) — a fixed self-request through the same read-only loop.
SUGGESTION_PROMPT = (
    "Review the current server state and propose 2-4 concrete, prioritized improvements. Be "
    "specific — reference actual channels, roles, or patterns you see, not generic advice."
)
LAST_RUN_META_KEY = "gigabrain_last_run_date"


async def _remember(
    store: Store, actor_id: int, channel_id: int | None, request: str, answer: str
) -> None:
    """Persist one completed turn so the next request in this channel has continuity."""
    if channel_id is None:
        return
    await store.add_gigabrain(actor_id, channel_id, "user", request)
    await store.add_gigabrain(actor_id, channel_id, "bot", answer)


async def handle_gigabrain_request(
    *,
    request: str,
    guild: Any,
    actor_id: int,
    llm: LLM,
    store: Store,
    ctx: ToolContext | None = None,
    channel_id: int | None = None,
) -> str:
    settings = ctx.settings if ctx is not None else None
    max_tool_calls = getattr(settings, "gigabrain_max_tool_calls", None) or MAX_TOOL_CALLS
    max_turns = getattr(settings, "gigabrain_max_turns", None) or MAX_TURNS

    await store.record_audit(
        actor_id=actor_id,
        brain="gigabrain",
        tool=None,
        args={"request": request},
        status=AuditStatus.OK,
        detail="request",
    )

    snap = await executors.snapshot(guild)
    tools = schemas.openai_tools(list(GIGABRAIN_TOOLS))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "Current server state:\n" + json.dumps(snap, default=str)},
    ]
    if channel_id is not None:
        for turn in await store.recent_gigabrain(actor_id, channel_id):
            role = "assistant" if turn["role"] == "bot" else "user"
            messages.append({"role": role, "content": turn["content"]})
    messages.append({"role": "user", "content": request})

    tool_calls_used = 0
    turns = 0
    try:
        while True:
            turns += 1
            if turns > max_turns:
                return "I couldn't finish that within my step budget."

            response = await llm.complete("gigabrain", messages, tools=tools)
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
                        brain="gigabrain",
                        tool=call.function.name,
                        args=None,
                        status=AuditStatus.DENIED,
                        detail="tool budget",
                    )
                    messages.append(
                        _tool_message(
                            call.id,
                            {
                                "error": (
                                    f"tool-call limit for this request reached ({max_tool_calls} "
                                    "calls). Tell the user to ask again to continue."
                                )
                            },
                        )
                    )
                    continue

                tool_calls_used += 1
                result, status, detail = await _run_tool(call, guild, ctx)
                await store.record_audit(
                    actor_id=actor_id,
                    brain="gigabrain",
                    tool=call.function.name,
                    args=_safe_args(call),
                    status=status,
                    detail=detail,
                )
                messages.append(_tool_message(call.id, result))
    except BudgetExceeded:
        await store.record_audit(
            actor_id=actor_id,
            brain="gigabrain",
            tool=None,
            args={"request": request},
            status=AuditStatus.ERROR,
            detail="daily token cap",
        )
        return "I've hit my daily token budget for gigabrain work. Try again tomorrow."
    except LLMConfigError as exc:
        return f"Gigabrain isn't configured yet ({exc})."


async def run_gigabrain_suggestion(
    *, client: Any, settings: Any, llm: LLM, store: Store
) -> dict[str, Any]:
    """Periodic, unprompted server-improvement suggestions — DMed to the owner.

    Self-gated on ``gigabrain_interval_days`` via a last-run date persisted in ``meta`` (survives
    restarts, unlike an in-memory guard), so a naive daily tick that just calls this unconditionally
    is safe — mirrors how ``run_digest_job`` decides "no new items" itself rather than the caller
    precomputing it. No conversation memory of its own (``channel_id=None``): this is a one-shot job
    like the digest, not a user-initiated exchange.
    """
    if settings.gigabrain_interval_days <= 0:
        return {"status": "periodic suggestions not configured"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).date()
    last_run = await store.get_meta(LAST_RUN_META_KEY)
    if last_run:
        elapsed = (today - datetime.date.fromisoformat(last_run)).days
        if elapsed < settings.gigabrain_interval_days:
            return {"status": "not due yet"}

    guild = client.get_guild(settings.guild_id)
    if guild is None:
        return {"status": f"guild {settings.guild_id} not visible"}

    ctx = ToolContext(llm=llm, store=store, settings=settings, client=client)
    try:
        answer = await handle_gigabrain_request(
            request=SUGGESTION_PROMPT,
            guild=guild,
            actor_id=settings.owner_id,
            llm=llm,
            store=store,
            ctx=ctx,
            channel_id=None,
        )
    except Exception:
        log.exception("periodic gigabrain suggestion failed")
        return {"status": "error running suggestion"}

    try:
        owner = await client.fetch_user(settings.owner_id)
        if len(answer) <= 4096:
            embed = discord.Embed(title="Giga Brain — periodic check-in", description=answer)
            await owner.send(embed=embed)
        else:
            file = discord.File(io.BytesIO(answer.encode("utf-8")), filename="gigabrain-checkin.md")
            await owner.send(
                content="Giga Brain's periodic check-in ran long — full report attached.",
                file=file,
            )
    except discord.DiscordException:
        log.exception("failed to DM owner with gigabrain suggestion")
        return {"status": "DM failed; suggestion not delivered"}

    await store.set_meta(LAST_RUN_META_KEY, today.isoformat())
    return {"status": "delivered"}


async def _run_tool(
    call: Any, guild: Any, ctx: ToolContext | None
) -> tuple[dict[str, Any], AuditStatus, str | None]:
    name = call.function.name
    if name not in GIGABRAIN_TOOLS:
        msg = f"{name} is not available in gigabrain mode"
        return {"error": msg}, AuditStatus.INVALID, "not allowlisted"

    # GIGABRAIN_TOOLS is built from real REGISTRY keys (test_gigabrain_tools_are_all_read_only_and
    # _confirm_free indexes REGISTRY[name] for every entry), so this is always present.
    spec = schemas.REGISTRY[name]

    try:
        raw = json.loads(call.function.arguments or "{}")
        args = spec.args_model.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        return {"error": f"invalid arguments: {exc}"}, AuditStatus.INVALID, "arg validation"

    if spec.needs_confirm(args):
        # Should be unreachable — GIGABRAIN_TOOLS is read-only by construction — but refuse rather
        # than silently mutate if that invariant is ever broken.
        msg = f"{name} requires confirmation, unsupported in gigabrain mode"
        return {"error": msg}, AuditStatus.INVALID, "confirm required"

    try:
        result = await executors.EXECUTORS[name](guild, args, ctx)
        return result, AuditStatus.OK, None
    except GuardError as exc:
        return {"error": str(exc)}, AuditStatus.INVALID, "guard"
    except Exception as exc:  # surfaced to the model as a structured result, not raised
        log.exception("executor for %s failed", name)
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
