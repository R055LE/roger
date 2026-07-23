"""Client, intents, dispatch, owner gate, and the ``/roger`` slash command.

Wires the skeleton and the admin brain: a non-privileged connection, the guild-scoped commands, the
owner gate with audit logging, and message routing. Owner requests (via ``/roger``, a DM, or an
@mention) go to the admin brain, which keeps short per-channel memory; ``/chat`` and non-owner chat
go to the ambient brain; a scheduled digest posts on its own loop.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from roger.brains.admin import handle_admin_request
from roger.brains.ambient import AmbientLimiter, handle_ambient
from roger.brains.digest import run_digest_job, seed_feeds_if_empty
from roger.config import Settings, load_settings
from roger.llm import LLM
from roger.store import AuditStatus, Store
from roger.tools.context import ToolContext

log = logging.getLogger("roger")

CANNED_DENY = "Sorry — Roger only takes admin requests from the server owner."
DISCORD_MAX = 2000
CONFIRM_TIMEOUT = 120


# --------------------------------------------------------------------------- logging


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # discord.py is noisy at INFO; keep the gateway chatter at WARNING.
    logging.getLogger("discord").setLevel(logging.WARNING)


def _truncate(text: str, limit: int = DISCORD_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


_MENTION_RE = re.compile(r"<@!?\d+>")


def _strip_mentions(content: str) -> str:
    return _MENTION_RE.sub("", content).strip()


# --------------------------------------------------------------------------- confirm flow


class _ConfirmView(discord.ui.View):
    """Owner-only ✅/❌ buttons for a pending change. Timeout counts as a deny."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self._owner_id = owner_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message("Not your call.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.edit_message(content="Approved.", view=None)
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.edit_message(content="Denied.", view=None)
        self.stop()


def _make_confirmer(
    send: Callable[..., Awaitable[Any]], owner_id: int
) -> Callable[[str], Awaitable[bool]]:
    async def confirm(diff: str) -> bool:
        view = _ConfirmView(owner_id)
        await send(content=f"**Confirm this change:**\n```\n{diff}\n```", view=view)
        await view.wait()  # returns on click or timeout
        return bool(view.value)  # None (timeout) -> False

    return confirm


# --------------------------------------------------------------------------- dispatch


class Route(Enum):
    IGNORE = "ignore"
    ADMIN_DM = "admin_dm"
    ADMIN_MENTION = "admin_mention"
    AMBIENT_DM = "ambient_dm"
    AMBIENT_MENTION = "ambient_mention"


def classify_message(message: discord.Message, *, owner_id: int, bot_user_id: int) -> Route:
    """Pure routing decision — no side effects, so it is unit-testable with fakes.

    Because the privileged ``message_content`` intent is OFF, guild messages that neither mention
    Roger nor arrive in a DM show up with empty content and are ignored by design.
    """
    if message.author.id == bot_user_id:
        return Route.IGNORE
    if not (message.content or "").strip():
        return Route.IGNORE
    if message.guild is None:  # DM
        return Route.ADMIN_DM if message.author.id == owner_id else Route.AMBIENT_DM
    if any(user.id == bot_user_id for user in message.mentions):
        # Owner @mentions reach the admin brain; everyone else gets ambient.
        return Route.ADMIN_MENTION if message.author.id == owner_id else Route.AMBIENT_MENTION
    return Route.IGNORE


# --------------------------------------------------------------------------- permissions

# The gateway permissions Roger's tools actually need — matches the invite set in deploy/README.md.
REQUIRED_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channels"),
    ("manage_channels", "Manage Channels"),
    ("manage_roles", "Manage Roles"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
)


def _missing_permissions(perms: discord.Permissions) -> list[str]:
    """Names of the required permissions Roger is *not* granted (pure, so it unit-tests cleanly)."""
    return [label for attr, label in REQUIRED_PERMISSIONS if not getattr(perms, attr)]


# --------------------------------------------------------------------------- client


class RogerClient(discord.Client):
    def __init__(self, settings: Settings, store: Store, llm: LLM) -> None:
        # Intents.default() has message_content, members, and presences OFF — invariant §2.1.
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.store = store
        self.llm = llm
        self.tree = app_commands.CommandTree(self)
        self._guild = discord.Object(id=settings.guild_id)
        self.ambient_limiter = AmbientLimiter(
            settings.ambient_rate_per_user,
            settings.ambient_rate_window_s,
            settings.ambient_global_hourly,
        )
        self._perms_checked = False

    async def setup_hook(self) -> None:
        self._assert_non_privileged()
        _register_commands(self)
        # Guild-scoped sync is instant and never leaks the command to other servers.
        await self.tree.sync(guild=self._guild)
        # Bootstrap the curated feed list from DIGEST_FEEDS on first run; then the store owns it.
        seeded = await seed_feeds_if_empty(self.store, self.settings)
        if seeded:
            log.info("seeded %d feed(s) from DIGEST_FEEDS into the store", seeded)
        # Start the daily loop whenever a channel is configured — Roger can curate feeds at runtime.
        if self.settings.digest_channel_id is not None:
            self._digest_loop.change_interval(
                time=datetime.time(
                    hour=self.settings.digest_hour, tzinfo=ZoneInfo(self.settings.tz)
                )
            )
            self._digest_loop.start()
            log.info(
                "digest scheduled daily at %02d:00 %s", self.settings.digest_hour, self.settings.tz
            )

    def _assert_non_privileged(self) -> None:
        i = self.intents
        if i.message_content or i.members or i.presences:
            raise RuntimeError(
                "privileged intents must stay off "
                f"(message_content={i.message_content}, members={i.members}, "
                f"presences={i.presences})"
            )

    async def on_ready(self) -> None:
        log.info("roger online as %s (guild=%s)", self.user, self.settings.guild_id)
        self._warn_on_missing_permissions()

    def _warn_on_missing_permissions(self) -> None:
        # on_ready fires again on every reconnect; report the permission state once per process.
        if self._perms_checked:
            return
        guild = self.get_guild(self.settings.guild_id)
        if guild is None or guild.me is None:
            log.warning("can't verify permissions — guild %s not visible", self.settings.guild_id)
            return
        self._perms_checked = True
        missing = _missing_permissions(guild.me.guild_permissions)
        if missing:
            log.warning(
                "missing Discord permissions: %s — admin tools that need them will 403; re-invite "
                "with the set in deploy/README.md (never Administrator)",
                ", ".join(missing),
            )
        else:
            log.info("permission check ok — all required scopes granted")

    async def on_message(self, message: discord.Message) -> None:
        route = classify_message(
            message, owner_id=self.settings.owner_id, bot_user_id=self.user.id
        )
        if route in (Route.ADMIN_DM, Route.ADMIN_MENTION):
            content = message.content
            if route is Route.ADMIN_MENTION:
                content = _strip_mentions(content)  # strip the mention first
                if not content:
                    return
            reply = await self._run_admin(
                content, message.author.id, message.channel.id, message.channel.send
            )
            await message.channel.send(_truncate(reply))
        elif route in (Route.AMBIENT_DM, Route.AMBIENT_MENTION):
            content = message.content
            if route is Route.AMBIENT_MENTION:
                content = _strip_mentions(content)  # §5: strip the mention first
                if not content:
                    return
            reply = await handle_ambient(
                content=content,
                user_id=message.author.id,
                channel_id=message.channel.id,
                llm=self.llm,
                store=self.store,
                limiter=self.ambient_limiter,
            )
            if reply:
                await message.channel.send(_truncate(reply))

    async def _run_admin(
        self,
        request: str,
        actor_id: int,
        channel_id: int,
        send: Callable[..., Awaitable[Any]],
    ) -> str:
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            return "I can't see the configured guild right now."
        confirm = _make_confirmer(send, self.settings.owner_id)
        ctx = ToolContext(llm=self.llm, store=self.store, settings=self.settings, client=self)
        try:
            return await handle_admin_request(
                request=request,
                guild=guild,
                actor_id=actor_id,
                llm=self.llm,
                store=self.store,
                confirm=confirm,
                ctx=ctx,
                channel_id=channel_id,
            )
        except Exception:
            # Never leave a deferred interaction hanging — reply, then surface it in the logs.
            log.exception("admin request failed for actor %s", actor_id)
            return "Something went wrong handling that — check the logs."

    @tasks.loop(time=datetime.time(hour=8))
    async def _digest_loop(self) -> None:
        result = await run_digest_job(
            client=self, settings=self.settings, llm=self.llm, store=self.store
        )
        log.info("scheduled digest: %s", result.get("status"))

    @_digest_loop.before_loop
    async def _before_digest(self) -> None:
        await self.wait_until_ready()


def _register_commands(client: RogerClient) -> None:
    @client.tree.command(
        name="roger",
        description="Ask Roger to manage the server (owner only).",
        guild=client._guild,
    )
    @app_commands.describe(request="What you want Roger to do, in plain language.")
    async def roger_cmd(interaction: discord.Interaction, request: str) -> None:
        await _handle_roger_request(client, interaction, request)

    @client.tree.command(
        name="chat",
        description="Chat with Roger's ambient persona.",
        guild=client._guild,
    )
    @app_commands.describe(message="Something to say to Roger.")
    async def chat_cmd(interaction: discord.Interaction, message: str) -> None:
        await _handle_chat(client, interaction, message)


async def _handle_roger_request(
    client: RogerClient, interaction: discord.Interaction, request: str
) -> None:
    user_id = interaction.user.id

    # Owner gate (§2.3): runs before any LLM dispatch — zero tokens spent on a non-owner.
    if user_id != client.settings.owner_id:
        await client.store.record_audit(
            actor_id=user_id,
            brain="admin",
            tool=None,
            args={"request": request},
            status=AuditStatus.GATE_REJECTED,
            detail="non-owner /roger",
        )
        await interaction.response.send_message(CANNED_DENY, ephemeral=True)
        log.info("gate rejected /roger from non-owner %s", user_id)
        return

    # Owner path. defer() up front — model + tool round-trips exceed Discord's 3s ack window.
    await interaction.response.defer(thinking=True)
    reply = await client._run_admin(
        request, user_id, interaction.channel_id, interaction.followup.send
    )
    await interaction.followup.send(_truncate(reply))


async def _handle_chat(
    client: RogerClient, interaction: discord.Interaction, message: str
) -> None:
    # Ambient on demand — open to anyone, no owner gate; ambient has no tools or authority.
    await interaction.response.defer(thinking=True)
    reply = await handle_ambient(
        content=message,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
        llm=client.llm,
        store=client.store,
        limiter=client.ambient_limiter,
    )
    await interaction.followup.send(_truncate(reply or "…"))


# --------------------------------------------------------------------------- entrypoint


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    store = await Store(settings.db_path).open()
    llm = LLM(settings, store)
    client = RogerClient(settings, store, llm)
    try:
        await client.start(settings.discord_token)
    finally:
        await client.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
