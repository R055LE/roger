"""Client, intents, dispatch, owner gate, and the ``/roger`` slash command.

P1 wires the skeleton: a non-privileged connection, the guild-scoped command, the owner gate with
audit logging, and message routing. The brains it routes to arrive in later phases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from enum import Enum

import discord
from discord import app_commands

from roger.config import Settings, load_settings
from roger.store import AuditStatus, Store

log = logging.getLogger("roger")

CANNED_DENY = "Sorry — Roger only takes admin requests from the server owner."


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


# --------------------------------------------------------------------------- dispatch


class Route(Enum):
    IGNORE = "ignore"
    ADMIN_DM = "admin_dm"
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
        return Route.AMBIENT_MENTION
    return Route.IGNORE


# --------------------------------------------------------------------------- client


class RogerClient(discord.Client):
    def __init__(self, settings: Settings, store: Store) -> None:
        # Intents.default() has message_content, members, and presences OFF — invariant §2.1.
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self._guild = discord.Object(id=settings.guild_id)

    async def setup_hook(self) -> None:
        self._assert_non_privileged()
        _register_commands(self)
        # Guild-scoped sync is instant and never leaks the command to other servers.
        await self.tree.sync(guild=self._guild)

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

    async def on_message(self, message: discord.Message) -> None:
        route = classify_message(
            message, owner_id=self.settings.owner_id, bot_user_id=self.user.id
        )
        if route is Route.IGNORE:
            return
        # Brains land in later phases; P1 only proves the routing is correct.
        log.debug("routed message from %s -> %s", message.author.id, route.value)
        # TODO(P2): Route.ADMIN_DM  -> admin brain
        # TODO(P4): Route.AMBIENT_DM / Route.AMBIENT_MENTION -> ambient brain


def _register_commands(client: RogerClient) -> None:
    @client.tree.command(
        name="roger",
        description="Ask Roger to manage the server (owner only).",
        guild=client._guild,
    )
    @app_commands.describe(request="What you want Roger to do, in plain language.")
    async def roger_cmd(interaction: discord.Interaction, request: str) -> None:
        await _handle_roger_request(client, interaction, request)


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

    # Owner path. defer() up front — real model calls will exceed Discord's 3s ack window.
    await interaction.response.defer(thinking=True)
    await client.store.record_audit(
        actor_id=user_id,
        brain="admin",
        tool=None,
        args={"request": request},
        status=AuditStatus.OK,
        detail="P1 echo",
    )
    await interaction.followup.send(
        f"(P1 skeleton) Received: {request!r}. The admin brain comes online in P2."
    )


# --------------------------------------------------------------------------- entrypoint


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    store = await Store(settings.db_path).open()
    client = RogerClient(settings, store)
    try:
        await client.start(settings.discord_token)
    finally:
        await client.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
