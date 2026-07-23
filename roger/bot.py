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
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from roger import metrics
from roger.brains.admin import handle_admin_request
from roger.brains.ambient import AmbientLimiter, handle_ambient
from roger.brains.digest import run_digest_job, seed_feeds_if_empty
from roger.config import Settings, load_settings
from roger.health import HEARTBEAT_PATH
from roger.llm import LLM
from roger.store import AuditStatus, Store
from roger.tools.context import ToolContext

log = logging.getLogger("roger")

CANNED_DENY = "Sorry — Roger only takes admin requests from the server owner."
DISCORD_MAX = 2000
CONFIRM_TIMEOUT = 120

# Deployed build identity, baked into the image by the release workflow (Dockerfile ARG →
# ROGER_VERSION). It's image metadata, not host-injected config, so it's read straight from the
# environment rather than through Settings/compose. Defaults to "dev" for local runs.
ROGER_VERSION = os.getenv("ROGER_VERSION", "dev")

# Liveness (§ backlog 1.4): the heartbeat loop refreshes HEARTBEAT_PATH; the Dockerfile HEALTHCHECK
# reads it via `python -m roger.health`. Kept well under health.MAX_AGE_S so a beat can be missed.
HEARTBEAT_INTERVAL_S = 60

# Metrics (§ backlog 3.1): how often the SQLite-sourced gauges are refreshed for Prometheus.
METRICS_REFRESH_S = 30

# Ops watchdog (§ backlog 1.2): a periodic health sweep pushes deduped alerts to the ops channel.
WATCHDOG_INTERVAL_MIN = 10
BUDGET_ALERT_FRACTION = 0.8  # warn once a brain crosses this share of its daily token cap
_DAY_S = 24 * 3600  # budget/digest alerts: at most one per day (naturally re-armed by the date key)
_PERM_ALERT_COOLDOWN_S = 6 * 3600  # a missing scope re-reminds every 6h while it stays broken


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


# --------------------------------------------------------------------------- status & ops

_BRAINS = ("admin", "ambient", "digest")


def _daily_caps(settings: Settings) -> dict[str, int]:
    """Per-brain daily token caps, keyed by brain (shared by /status and the watchdog)."""
    return {
        "admin": settings.daily_tokens_admin,
        "ambient": settings.daily_tokens_ambient,
        "digest": settings.daily_tokens_digest,
    }


def _boot_header(version: str, missing: list[str]) -> str:
    """Header of the boot self-report (pure): health glyph + the deployed build.

    The full state block — permissions, token/dollar spend, digest schedule, recent actions — is
    rendered separately by ``gather_status`` and appended under this header, so the ops channel gets
    a complete snapshot on every deploy instead of a bare "online" line. A missing required scope
    adds an actionable re-invite hint here (it's the one thing you must fix by hand off-box).
    """
    if missing:
        return (
            f"⚠️ **roger online** · `{version}`\n"
            f"Missing permissions: **{', '.join(missing)}** — admin tools that need them will "
            "fail; re-invite per `deploy/README.md`."
        )
    return f"✅ **roger online** · `{version}`"


def _hhmm(ts: float, tz: str) -> str:
    return datetime.datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M")


def _format_status(
    *,
    guild_name: str,
    missing_perms: list[str],
    usage: dict[str, int],
    caps: dict[str, int],
    cost: dict[str, float],
    feeds_count: int,
    recent_audit: list[dict[str, Any]],
    digest_hour: int,
    digest_configured: bool,
    tz: str,
) -> str:
    """Render the /status readout body (pure). The caller wraps it in a code block."""
    perms = "OK" if not missing_perms else "MISSING: " + ", ".join(missing_perms)
    lines = [
        f"roger status — {guild_name}",
        f"permissions: {perms}",
        "spend today (tokens used / cap · cost):",
    ]
    total_cost = 0.0
    for brain in _BRAINS:
        spent = cost.get(brain, 0.0)
        total_cost += spent
        lines.append(
            f"  {brain:<8}{usage.get(brain, 0):>8,} / {caps.get(brain, 0):<8,}  ${spent:.4f}"
        )
    lines.append(f"  {'total':<27}  ${total_cost:.4f}")
    digest = f"{digest_hour:02d}:00 {tz}" if digest_configured else "unconfigured"
    lines.append(f"feeds: {feeds_count}   digest: {digest}")
    if recent_audit:
        lines.append("recent actions:")
        for row in recent_audit:
            when = _hhmm(row["ts"], tz)
            tool = row.get("tool") or "—"
            detail = f" ({row['detail']})" if row.get("detail") else ""
            # 16-wide: the longest tool name (`set_permissions`) is 15, so a gap is always left.
            lines.append(f"  {when}  {tool:<16}{row.get('status', '?')}{detail}")
    return "\n".join(lines)


async def gather_status(*, store: Store, settings: Settings, guild: Any) -> str:
    """Gather live status and render the /status body. Kept client-free so it unit-tests alone."""
    guild_name = guild.name if guild is not None else str(settings.guild_id)
    missing = (
        _missing_permissions(guild.me.guild_permissions)
        if guild is not None and guild.me is not None
        else []
    )
    usage = {brain: await store.usage_today(brain) for brain in _BRAINS}
    cost = {brain: await store.cost_today(brain) for brain in _BRAINS}
    caps = _daily_caps(settings)
    return _format_status(
        guild_name=guild_name,
        missing_perms=missing,
        usage=usage,
        caps=caps,
        cost=cost,
        feeds_count=await store.count_feeds(),
        recent_audit=await store.fetch_audit(limit=8),
        digest_hour=settings.digest_hour,
        digest_configured=settings.digest_channel_id is not None,
        tz=settings.tz,
    )


# --------------------------------------------------------------------------- ops alerting


class OpsNotifier:
    """Dedupes ops-channel alerts so a persistent condition pings once per cooldown, not every tick.

    State is in-memory only. A restart re-arms every key, which is fine: startup re-evaluates live
    state and the boot self-report already re-announces health, so nothing is silently lost.
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send
        self._clock = clock
        self._last: dict[str, float] = {}

    async def alert(self, key: str, message: str, *, cooldown_s: float) -> bool:
        """Post the message unless this key already fired within its cooldown window."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < cooldown_s:
            return False
        # Mark before send; _post_ops swallows its own failures and never raises.
        self._last[key] = now
        await self._send(message)
        return True


def _budget_alert(
    brain: str, used: int, cap: int, cost: float, *, fraction: float = BUDGET_ALERT_FRACTION
) -> str | None:
    """Alert text when ``brain`` crosses ``fraction`` of its daily token cap, else None (pure)."""
    if cap <= 0 or used < fraction * cap:
        return None
    tokens = f"{used:,} / {cap:,} tokens today (${cost:.4f})"
    if used >= cap:
        return f"⚠️ **{brain} budget exhausted** — {tokens}. Calls refused until the daily reset."
    pct = round(100 * used / cap)
    return f"⚠️ **{brain} budget {pct}%** — {tokens}. Approaching the daily cap."


# Digest statuses that mean "ran fine, nothing to flag"; anything else is worth an ops ping.
_DIGEST_OK_PREFIXES = ("posted", "no new items")


def _digest_problem(status: str) -> str | None:
    """The digest status if it signals a problem worth alerting on, else None (pure)."""
    if any(status.startswith(prefix) for prefix in _DIGEST_OK_PREFIXES):
        return None
    return status


def _touch_heartbeat(path: str = HEARTBEAT_PATH) -> None:
    """Rewrite the liveness file so its mtime is now (content is informational only)."""
    with open(path, "w") as handle:
        handle.write(str(time.time()))


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
        self._ops = OpsNotifier(self._post_ops)
        self._last_prune_date: str | None = None
        self._metrics_server: Any = None

    async def setup_hook(self) -> None:
        self._assert_non_privileged()
        _register_commands(self)
        # Guild-scoped sync is instant and never leaks the command to other servers.
        await self.tree.sync(guild=self._guild)
        # Bootstrap the curated feed list from DIGEST_FEEDS on first run; then the store owns it.
        seeded = await seed_feeds_if_empty(self.store, self.settings)
        if seeded:
            log.info("seeded %d feed(s) from DIGEST_FEEDS into the store", seeded)
        await self._maybe_prune()  # tidy expired rows on boot; the watchdog repeats it daily
        self._heartbeat.start()  # liveness for the Dockerfile HEALTHCHECK (always on)
        if self.settings.metrics_port:
            self._metrics_server = metrics.start_server(self.settings.metrics_port)
            await metrics.refresh(self.store, self.settings, ROGER_VERSION)
            self._metrics_refresh.start()
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
        # The watchdog only earns its keep when there's an ops channel to post alerts to.
        if self.settings.ops_channel_id is not None:
            self._watchdog.start()
            log.info("ops watchdog started (every %d min)", WATCHDOG_INTERVAL_MIN)

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
        await self._startup_report()

    async def _startup_report(self) -> None:
        # on_ready fires again on every reconnect; report once per process.
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
        header = _boot_header(ROGER_VERSION[:12], missing)
        body = await gather_status(store=self.store, settings=self.settings, guild=guild)
        await self._post_ops(f"{header}\n```\n{body}\n```")

    async def _post_ops(self, message: str) -> None:
        """Best-effort post to the ops channel; never let a failure here take down startup."""
        channel_id = self.settings.ops_channel_id
        if channel_id is None:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            log.warning("ops channel %s not found — skipping self-report", channel_id)
            return
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            log.exception("failed to post to ops channel %s", channel_id)

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
        status = str(result.get("status", ""))
        log.info("scheduled digest: %s", status)
        problem = _digest_problem(status)
        if problem:
            await self._ops.alert(
                f"digest:{time.strftime('%Y-%m-%d')}",
                f"⚠️ **digest problem** — {problem}",
                cooldown_s=_DAY_S,
            )

    @_digest_loop.before_loop
    async def _before_digest(self) -> None:
        await self.wait_until_ready()

    async def _maybe_prune(self) -> None:
        """Prune expired rows at most once per calendar day (called at boot and by the watchdog)."""
        today = time.strftime("%Y-%m-%d")
        if today == self._last_prune_date:
            return
        self._last_prune_date = today
        log.info("pruned expired rows: %s", await self.store.prune())

    @tasks.loop(minutes=WATCHDOG_INTERVAL_MIN)
    async def _watchdog(self) -> None:
        """Periodic health sweep → deduped ops alerts: permission loss and budget thresholds."""
        await self._maybe_prune()
        guild = self.get_guild(self.settings.guild_id)
        if guild is not None and guild.me is not None:
            missing = _missing_permissions(guild.me.guild_permissions)
            if missing:
                await self._ops.alert(
                    f"perms:{','.join(sorted(missing))}",
                    f"⚠️ **permission loss** — missing **{', '.join(missing)}**; "
                    "re-invite per `deploy/README.md`.",
                    cooldown_s=_PERM_ALERT_COOLDOWN_S,
                )
        caps = _daily_caps(self.settings)
        today = time.strftime("%Y-%m-%d")
        for brain in _BRAINS:
            message = _budget_alert(
                brain,
                await self.store.usage_today(brain),
                caps[brain],
                await self.store.cost_today(brain),
            )
            if message:
                await self._ops.alert(f"budget:{brain}:{today}", message, cooldown_s=_DAY_S)

    @_watchdog.before_loop
    async def _before_watchdog(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=HEARTBEAT_INTERVAL_S)
    async def _heartbeat(self) -> None:
        # A tick proves the event loop is turning; a wedge stops it and the file goes stale.
        _touch_heartbeat()

    @_heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=METRICS_REFRESH_S)
    async def _metrics_refresh(self) -> None:
        await metrics.refresh(self.store, self.settings, ROGER_VERSION)

    @_metrics_refresh.before_loop
    async def _before_metrics_refresh(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        # Stop the metrics WSGI thread before the loop tears down; then hand off to discord.py.
        if self._metrics_server is not None:
            self._metrics_server.shutdown()
            self._metrics_server = None
        await super().close()


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

    @client.tree.command(
        name="status",
        description="Roger's operational status: permissions, token spend, recent actions (owner).",
        guild=client._guild,
    )
    async def status_cmd(interaction: discord.Interaction) -> None:
        await _handle_status(client, interaction)


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


async def _handle_status(client: RogerClient, interaction: discord.Interaction) -> None:
    # Owner-only, read-only, ephemeral — a deterministic ops readout, no LLM spend.
    if interaction.user.id != client.settings.owner_id:
        await interaction.response.send_message(CANNED_DENY, ephemeral=True)
        return
    guild = client.get_guild(client.settings.guild_id)
    body = await gather_status(store=client.store, settings=client.settings, guild=guild)
    await interaction.response.send_message(f"```\n{body}\n```", ephemeral=True)


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
