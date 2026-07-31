"""On-demand smoke test against the real, live Discord API — not part of the hermetic pytest suite.

Exercises real network calls: a live gateway connection, real tool-executor REST calls, and real LLM
completions. It costs real tokens and needs real credentials, so it's meant to be run by hand, never
CI-gated — its flakiness (rate limits, model hiccups) should never block a deploy.

Safe by construction: ambient has no tools at all (§8), and gigabrain's tools are a fixed read-only
allowlist enforced twice (§12), so neither call can mutate the server. The admin brain is
deliberately NOT exercised here — it gets the full tool registry, and create_channel/create_role
aren't confirm-gated by default, so an open-ended smoke-test prompt could theoretically create real
state. Revisit once handle_admin_request can take a restricted tool subset.

Run against the deployed container (reuses its env — no new secrets, no separate checkout):
    docker exec "$(docker ps -q --filter name=roger)" python -m roger.smoke_test

Or locally against a decrypted env:
    sops exec-env roger.env python -m roger.smoke_test
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any

import discord

from roger.bot import _send_chunked, _send_report, gather_status
from roger.brains.ambient import AmbientLimiter, handle_ambient
from roger.brains.gigabrain import handle_gigabrain_request
from roger.config import Settings, load_settings
from roger.llm import LLM
from roger.store import Store
from roger.tools import executors
from roger.tools.context import ToolContext
from roger.tools.schemas import ServerStatsArgs

log = logging.getLogger("roger.smoke_test")

CONNECT_TIMEOUT_S = 120


@dataclass
class _Result:
    name: str
    ok: bool
    detail: str = ""


async def _check_status(*, store: Store, settings: Settings, guild: discord.Guild) -> _Result:
    try:
        body = await gather_status(store=store, settings=settings, guild=guild)
    except Exception as exc:
        return _Result("status", False, f"raised: {exc!r}")
    if "permissions:" not in body or "spend today" not in body:
        return _Result("status", False, f"unexpected shape:\n{body[:300]}")
    perm_line = next((line for line in body.splitlines() if line.startswith("permissions:")), "?")
    return _Result("status", True, perm_line.strip())


async def _check_snapshot(guild: discord.Guild) -> _Result:
    try:
        snap = await executors.snapshot(guild)
        stats = await executors.server_stats(guild, ServerStatsArgs(), None)
    except Exception as exc:
        return _Result("snapshot/server_stats", False, f"raised: {exc!r}")
    if not snap.get("channels") and not snap.get("categories"):
        return _Result(
            "snapshot/server_stats", False, "snapshot returned no channels or categories"
        )
    return _Result(
        "snapshot/server_stats",
        True,
        f"{len(snap.get('channels', []))} channels, {len(snap.get('roles', []))} roles, "
        f"{stats.get('members')} members",
    )


async def _check_ambient(
    *, llm: LLM, store: Store, settings: Settings, actor_id: int, channel_id: int | None
) -> _Result:
    if channel_id is None:
        return _Result("ambient", False, "no text channel visible to test in")
    limiter = AmbientLimiter(
        settings.ambient_rate_per_user,
        settings.ambient_rate_window_s,
        settings.ambient_global_hourly,
    )
    sent: list[Any] = []

    async def sink(content: Any = None, **_kwargs: Any) -> None:
        sent.append(content)

    try:
        reply = await handle_ambient(
            content="Say hello in exactly five words.",
            user_id=actor_id,
            channel_id=channel_id,
            llm=llm,
            store=store,
            limiter=limiter,
        )
    except Exception as exc:
        return _Result("ambient", False, f"raised: {exc!r}")
    if reply is None:
        return _Result(
            "ambient", False, "no reply (MODEL_AMBIENT unconfigured, or unexpectedly rate-limited)"
        )

    try:
        await _send_chunked(sink, reply)
    except Exception as exc:
        return _Result("ambient", False, f"_send_chunked raised: {exc!r}")
    if "".join(sent).replace("\n", "") != reply.replace("\n", ""):
        return _Result(
            "ambient", False, "chunked reconstruction lost content vs. the original reply"
        )
    return _Result("ambient", True, f"reply: {reply[:80]!r}")


async def _check_gigabrain(
    *,
    client: discord.Client,
    guild: discord.Guild,
    llm: LLM,
    store: Store,
    settings: Settings,
    actor_id: int,
    channel_id: int | None,
) -> _Result:
    ctx = ToolContext(llm=llm, store=store, settings=settings, client=client)
    try:
        reply = await handle_gigabrain_request(
            request="Give me a two-sentence first impression of this server.",
            guild=guild,
            actor_id=actor_id,
            llm=llm,
            store=store,
            ctx=ctx,
            channel_id=channel_id,
        )
    except Exception as exc:
        return _Result("gigabrain", False, f"raised: {exc!r}")
    if not reply or not reply.strip():
        return _Result("gigabrain", False, "empty reply")

    sent: list[Any] = []

    async def sink(content: Any = None, **kwargs: Any) -> None:
        sent.append(kwargs.get("file") if kwargs.get("file") is not None else content)

    try:
        await _send_report(sink, reply)
    except Exception as exc:
        return _Result("gigabrain", False, f"_send_report raised: {exc!r}")

    if len(reply) <= 2000:
        if sent != [reply]:
            return _Result("gigabrain", False, "short reply was not delivered as plain text")
        return _Result("gigabrain", True, f"{len(reply)} chars, delivered as text")

    file = sent[0] if sent else None
    if not isinstance(file, discord.File):
        return _Result("gigabrain", False, "long reply did not attach a file")
    content = file.fp.read().decode("utf-8")
    if content != reply:
        return _Result(
            "gigabrain", False, "attached file content didn't match the full reply (truncated?)"
        )
    return _Result("gigabrain", True, f"{len(reply)} chars, delivered as {file.filename}")


async def _run_checks(
    client: discord.Client, settings: Settings, store: Store, llm: LLM
) -> list[_Result]:
    guild = client.get_guild(settings.guild_id)
    if guild is None:
        return [_Result("guild visibility", False, f"guild {settings.guild_id} not visible")]

    results = [
        await _check_status(store=store, settings=settings, guild=guild),
        await _check_snapshot(guild),
    ]
    channel_id = guild.text_channels[0].id if guild.text_channels else None
    results.append(
        await _check_ambient(
            llm=llm,
            store=store,
            settings=settings,
            actor_id=settings.owner_id,
            channel_id=channel_id,
        )
    )
    results.append(
        await _check_gigabrain(
            client=client,
            guild=guild,
            llm=llm,
            store=store,
            settings=settings,
            actor_id=settings.owner_id,
            channel_id=channel_id,
        )
    )
    return results


def _print_results(results: list[_Result]) -> bool:
    all_ok = True
    for result in results:
        tag = "[ok]  " if result.ok else "[FAIL]"
        print(f"{tag} {result.name}: {result.detail}")
        all_ok = all_ok and result.ok
    return all_ok


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    settings = load_settings()
    store = await Store(settings.db_path).open()
    llm = LLM(settings, store)
    client = discord.Client(intents=discord.Intents.default())
    exit_code = 1

    @client.event
    async def on_ready() -> None:
        nonlocal exit_code
        try:
            print(f"connected as {client.user} — running checks against guild {settings.guild_id}")
            results = await _run_checks(client, settings, store, llm)
            ok = _print_results(results)
            costs = [await store.cost_today(brain) for brain in ("ambient", "gigabrain")]
            print(f"cumulative spend today, ambient+gigabrain: ${sum(costs):.4f}")
            exit_code = 0 if ok else 1
        except Exception:
            log.exception("smoke test crashed")
            exit_code = 1
        finally:
            await client.close()

    try:
        await asyncio.wait_for(client.start(settings.discord_token), timeout=CONNECT_TIMEOUT_S)
    except TimeoutError:
        print("[FAIL] timed out waiting for the gateway connection")
        exit_code = 1
    finally:
        await store.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
