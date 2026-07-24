"""Executors: the actual Discord API calls behind each tool.

``snapshot`` doubles as the pre-request server state fed to the admin model and as the
``list_structure`` tool result (§7). ``preview`` renders the exact change a confirm-gated tool
would make, so the owner approves against a real diff — not the model's paraphrase.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import discord
import feedparser

from roger.tools.context import ToolContext
from roger.tools.guard import (
    GuardError,
    check_no_duplicate,
    parse_color,
    resolve_one,
    sanitize_channel_name,
    sanitize_display_name,
)
from roger.tools.schemas import (
    AddFeedArgs,
    AddReactionArgs,
    CreateChannelArgs,
    CreateRoleArgs,
    EditChannelArgs,
    ListFeedsArgs,
    ListStructureArgs,
    MoveChannelArgs,
    PostMessageArgs,
    RemoveFeedArgs,
    RunDigestArgs,
    ServerStatsArgs,
    SetNicknameArgs,
    SetPermissionsArgs,
    SetPresenceArgs,
    SuggestFeedsArgs,
)

# --------------------------------------------------------------------------- snapshot


def _overwrite_summary(overwrites: dict[Any, discord.PermissionOverwrite]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for target, overwrite in overwrites.items():
        name = getattr(target, "name", str(target))
        allow = [perm for perm, value in overwrite if value is True]
        deny = [perm for perm, value in overwrite if value is False]
        summary[name] = {"allow": allow, "deny": deny}
    return summary


async def snapshot(guild: discord.Guild, *, detailed: bool = False) -> dict[str, Any]:
    """Server state for the admin model.

    The lean form (default) is the per-request context fed to the model on every turn, so it omits
    the two costly, usually-irrelevant fields — channel topics and the permission-overwrite matrix.
    ``list_structure`` asks for ``detailed=True`` when the model actually needs them.
    """
    categories = [{"id": c.id, "name": c.name} for c in guild.categories]

    channels: list[dict[str, Any]] = []
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            kind, category, topic = "category", None, None
        elif isinstance(channel, discord.TextChannel):
            kind = "text"
            category = channel.category.name if channel.category else None
            topic = channel.topic
        elif isinstance(channel, discord.VoiceChannel):
            kind = "voice"
            category = channel.category.name if channel.category else None
            topic = None
        else:
            continue
        entry: dict[str, Any] = {
            "id": channel.id,
            "name": channel.name,
            "kind": kind,
            "category": category,
        }
        if detailed:
            entry["topic"] = (topic or "")[:120] or None
            entry["overwrites"] = _overwrite_summary(channel.overwrites)
        channels.append(entry)

    if detailed:
        roles = [
            {"id": r.id, "name": r.name, "position": r.position, "color": str(r.color)}
            for r in guild.roles
        ]
    else:
        roles = [{"id": r.id, "name": r.name} for r in guild.roles]
    return {"categories": categories, "channels": channels, "roles": roles}


async def list_structure(
    guild: discord.Guild, args: ListStructureArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    # The tool the model calls when it wants the full picture — topics and permission overwrites.
    return await snapshot(guild, detailed=True)


# --------------------------------------------------------------------------- resolution


def _resolve_editable_channel(guild: discord.Guild, query: str) -> tuple[Any, str]:
    """Resolve a text/voice/category channel and report its kind — no ``isinstance`` needed."""
    buckets = (
        ("text", guild.text_channels),
        ("voice", guild.voice_channels),
        ("category", guild.categories),
    )
    items = [(c.id, c.name) for _, collection in buckets for c in collection]
    channel_id, _ = resolve_one(query, items)
    for kind, collection in buckets:
        for channel in collection:
            if channel.id == channel_id:
                return channel, kind
    raise GuardError(f"channel {query!r} vanished")


async def _resolve_target(guild: discord.Guild, query: str) -> discord.Role | discord.Member:
    q = query.strip()
    if q.casefold() in ("@everyone", "everyone"):
        return guild.default_role
    if q.casefold() in ("self", "me", "yourself", "roger"):
        return guild.me  # let the owner say "add yourself" and have it reliably mean the bot
    if q.isdigit():
        role = guild.get_role(int(q))
        if role is not None:
            return role
        try:
            return await guild.fetch_member(int(q))
        except discord.NotFound as exc:
            raise GuardError(f"no role or member with id {q}") from exc
    role_id, _ = resolve_one(q, [(r.id, r.name) for r in guild.roles])
    role = guild.get_role(role_id)
    if role is None:
        raise GuardError(f"role {query!r} vanished")
    return role


# --------------------------------------------------------------------------- mutations


async def _creation_overwrites(
    guild: discord.Guild, *, read_only: bool, private: bool, grants: list
) -> dict[Any, discord.PermissionOverwrite]:
    """Build the overwrite map for a brand-new channel from its access intent.

    A new channel has no members and no history, so restricting it at creation has nil blast radius
    (§2.8). Whenever we restrict @everyone we also keep Roger's own access, or it would lock itself
    out of a channel it just made — @everyone includes the bot.
    """
    overwrites: dict[Any, discord.PermissionOverwrite] = {}
    everyone_bits: dict[str, bool] = {}
    if private:
        everyone_bits["view_channel"] = False
    if read_only:
        everyone_bits["send_messages"] = False
    if everyone_bits:
        overwrites[guild.default_role] = discord.PermissionOverwrite(**everyone_bits)
        overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    for grant in grants:
        target = await _resolve_target(guild, grant.role)
        overwrites[target] = discord.PermissionOverwrite(**dict.fromkeys(grant.allow, True))
    return overwrites


async def create_channel(
    guild: discord.Guild, args: CreateChannelArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    if args.kind == "category":
        if args.category is not None:
            raise GuardError("a category can't be nested under another category")
        if args.read_only:
            raise GuardError("read_only is text-only; hide a whole category with private/grants")
        if args.topic is not None:
            raise GuardError("only text channels have a topic")
        name = sanitize_display_name(args.name)
        check_no_duplicate("category", name, [c.name for c in guild.categories])
        # Categories take overwrites too — a private admin category hides itself and its synced
        # children from @everyone, and grants let specific roles (and Roger) back in.
        overwrites = await _creation_overwrites(
            guild, read_only=False, private=args.private, grants=args.grants
        )
        created = await guild.create_category(name=name, overwrites=overwrites)
        return {
            "created": "category",
            "id": created.id,
            "name": created.name,
            "private": args.private,
            "grants": [g.role for g in args.grants],
        }

    category_obj = None
    if args.category is not None:
        cat_id, _ = resolve_one(args.category, [(c.id, c.name) for c in guild.categories])
        category_obj = guild.get_channel(cat_id)

    if args.kind == "text":
        name = sanitize_channel_name(args.name)
        check_no_duplicate("text channel", name, [c.name for c in guild.text_channels])
        overwrites = await _creation_overwrites(
            guild, read_only=args.read_only, private=args.private, grants=args.grants
        )
        created = await guild.create_text_channel(
            name=name, category=category_obj, topic=args.topic, overwrites=overwrites
        )
        return {
            "created": "text",
            "id": created.id,
            "name": created.name,
            "category": category_obj.name if category_obj else None,
            "read_only": args.read_only,
            "private": args.private,
            "grants": [g.role for g in args.grants],
        }

    if args.read_only:
        raise GuardError("read_only applies to text channels only (voice has no send)")
    name = sanitize_display_name(args.name)
    check_no_duplicate("voice channel", name, [c.name for c in guild.voice_channels])
    overwrites = await _creation_overwrites(
        guild, read_only=False, private=args.private, grants=args.grants
    )
    created = await guild.create_voice_channel(
        name=name, category=category_obj, overwrites=overwrites
    )
    return {
        "created": "voice",
        "id": created.id,
        "name": created.name,
        "category": category_obj.name if category_obj else None,
        "private": args.private,
        "grants": [g.role for g in args.grants],
    }


async def create_role(
    guild: discord.Guild, args: CreateRoleArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    name = sanitize_display_name(args.name)
    check_no_duplicate("role", name, [r.name for r in guild.roles])
    kwargs: dict[str, Any] = {
        "name": name,
        "permissions": discord.Permissions.none(),  # invariant §2.6 — always zero
        "hoist": args.hoist,
        "mentionable": args.mentionable,
    }
    if args.color:
        kwargs["color"] = discord.Color(parse_color(args.color))
    role = await guild.create_role(**kwargs)
    return {
        "created": "role",
        "id": role.id,
        "name": role.name,
        "permissions": role.permissions.value,
    }


async def set_permissions(
    guild: discord.Guild, args: SetPermissionsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    channel, _ = _resolve_editable_channel(guild, args.channel)  # text, voice, or category
    applied: list[dict[str, Any]] = []
    hides_from_everyone = False
    for overwrite in args.overwrites:
        target = await _resolve_target(guild, overwrite.target)
        permission_overwrite = discord.PermissionOverwrite(
            **dict.fromkeys(overwrite.allow, True),
            **dict.fromkeys(overwrite.deny, False),
        )
        await channel.set_permissions(target, overwrite=permission_overwrite)
        applied.append(
            {
                "target": getattr(target, "name", str(target)),
                "allow": list(overwrite.allow),
                "deny": list(overwrite.deny),
            }
        )
        if target == guild.default_role and "view_channel" in overwrite.deny:
            hides_from_everyone = True
    if hides_from_everyone:
        # @everyone includes the bot — keep Roger's own access or it locks itself out (§2.8).
        await channel.set_permissions(
            guild.me, overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True)
        )
        applied.append({"target": "Roger", "allow": ["view_channel", "send_messages"], "deny": []})
    return {"channel": channel.name, "applied": applied}


async def edit_channel(
    guild: discord.Guild, args: EditChannelArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    channel, kind = _resolve_editable_channel(guild, args.channel)
    changes: dict[str, Any] = {}
    result: dict[str, Any] = {"channel": channel.name, "kind": kind}

    if args.name is not None:
        if kind == "text":
            new_name = sanitize_channel_name(args.name)
            pool = [c.name for c in guild.text_channels if c.id != channel.id]
        else:
            new_name = sanitize_display_name(args.name)
            source = guild.voice_channels if kind == "voice" else guild.categories
            pool = [c.name for c in source if c.id != channel.id]
        check_no_duplicate("category" if kind == "category" else f"{kind} channel", new_name, pool)
        changes["name"] = new_name
        result["name"] = new_name

    if args.topic is not None:
        if kind != "text":
            raise GuardError("only text channels have a topic")
        changes["topic"] = args.topic
        result["topic"] = args.topic

    if args.category is not None:
        if kind == "category":
            raise GuardError("a category can't be nested under another category")
        cat_id, cat_name = resolve_one(args.category, [(c.id, c.name) for c in guild.categories])
        changes["category"] = guild.get_channel(cat_id)
        result["category"] = cat_name

    await channel.edit(**changes)
    result["edited"] = True
    return result


async def post_message(
    guild: discord.Guild, args: PostMessageArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    channel, kind = _resolve_editable_channel(guild, args.channel)
    if kind != "text":
        raise GuardError("can only post to a text channel")
    # Suppress @everyone/@here and role/user pings — Roger never mass-mentions for the owner.
    await channel.send(args.content, allowed_mentions=discord.AllowedMentions.none())
    return {"posted": True, "channel": channel.name, "chars": len(args.content)}


def _same_category(a: Any, b: Any) -> bool:
    """Do two channels sit under the same category? (Uncategorized counts as the same group.)"""
    ac, bc = getattr(a, "category", None), getattr(b, "category", None)
    return (ac.id if ac else None) == (bc.id if bc else None)


async def move_channel(
    guild: discord.Guild, args: MoveChannelArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    channel, kind = _resolve_editable_channel(guild, args.channel)
    if args.position == "top":
        await channel.move(beginning=True)
        anchor = "top"
    elif args.position == "bottom":
        await channel.move(end=True)
        anchor = "bottom"
    else:
        ref_query = args.before if args.before is not None else args.after
        ref, ref_kind = _resolve_editable_channel(guild, ref_query)
        if ref.id == channel.id:
            raise GuardError("can't move a channel relative to itself")
        if (kind == "category") != (ref_kind == "category"):
            raise GuardError("order a category next to a category, a channel next to a channel")
        # Discord groups channels under their category, so a cross-category before/after renders
        # nonsensically. Keep the move legible: same category (or both uncategorized).
        if kind != "category" and not _same_category(channel, ref):
            raise GuardError(
                "before/after must name a channel in the same category — "
                "move it into that category first with edit_channel"
            )
        if args.before is not None:
            await channel.move(before=ref)
            anchor = f"before {ref.name}"
        else:
            await channel.move(after=ref)
            anchor = f"after {ref.name}"
    return {"moved": channel.name, "kind": kind, "position": anchor}


async def run_digest(
    guild: discord.Guild, args: RunDigestArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    if ctx is None or ctx.settings is None:
        return {"status": "digest unavailable in this context"}
    from roger.brains.digest import run_digest_job

    return await run_digest_job(
        client=ctx.client, settings=ctx.settings, llm=ctx.llm, store=ctx.store
    )


# --------------------------------------------------------------------------- digest feeds


async def validate_feed(url: str) -> dict[str, Any]:
    """Fetch a URL and confirm it parses as a live RSS/Atom feed. Never raises."""
    try:
        parsed = await asyncio.to_thread(feedparser.parse, url)
    except Exception as exc:  # DNS failure, bad URL, etc.
        return {"url": url, "ok": False, "error": f"fetch failed: {exc}"}
    status = parsed.get("status")
    if status is not None and status >= 400:
        return {"url": url, "ok": False, "error": f"HTTP {status}"}
    # feedparser sets a non-empty ``version`` (e.g. "rss20", "atom10") only for a recognized feed.
    if not parsed.get("version"):
        return {"url": url, "ok": False, "error": "not a recognized RSS/Atom feed"}
    title = parsed.feed.get("title") if parsed.get("feed") else None
    return {"url": url, "ok": True, "title": title, "entries": len(parsed.entries)}


def _need_store(ctx: ToolContext | None) -> Any:
    if ctx is None or ctx.store is None:
        raise GuardError("the feed store is unavailable in this context")
    return ctx.store


async def suggest_feeds(
    guild: discord.Guild, args: SuggestFeedsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    # Vet candidate URLs against the live web without committing. The model proposes; this grounds.
    candidates = await asyncio.gather(*(validate_feed(url) for url in args.urls))
    return {"candidates": list(candidates)}


async def add_feed(
    guild: discord.Guild, args: AddFeedArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    checked = await validate_feed(args.url)
    if not checked["ok"]:
        return {"added": False, "url": args.url, "error": checked["error"]}
    added = await store.add_feed(args.url, checked.get("title"))
    return {
        "added": added,
        "url": args.url,
        "title": checked.get("title"),
        "entries": checked.get("entries"),
        "note": None if added else "already in the feed list",
    }


async def remove_feed(
    guild: discord.Guild, args: RemoveFeedArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    removed = await store.remove_feed(args.url)
    return {
        "removed": removed,
        "url": args.url,
        "note": None if removed else "no feed with that exact URL (call list_feeds first)",
    }


async def list_feeds(
    guild: discord.Guild, args: ListFeedsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    rows = await store.list_feeds()
    return {
        "feeds": [{"url": r["url"], "title": r["title"]} for r in rows],
        "count": len(rows),
    }


# --------------------------------------------------------------------------- toys (self / read)

# Where the persisted presence "outfit" lives in the meta table. bot.py reads this key on boot to
# reapply it — Discord clears presence on every reconnect (i.e. every pull-based redeploy).
PRESENCE_META_KEY = "presence"

_ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
_STATUS_VALUES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}


def _build_activity(activity: str | None, text: str | None) -> discord.Activity | None:
    if not activity or activity == "none" or not text:
        return None
    return discord.Activity(type=_ACTIVITY_TYPES[activity], name=text)


async def apply_presence(client: Any, state: dict[str, Any]) -> None:
    """Push a stored presence dict onto the gateway. Shared by the tool and bot.py's reapply."""
    status = _STATUS_VALUES.get(state.get("status") or "online", discord.Status.online)
    activity = _build_activity(state.get("activity"), state.get("text"))
    await client.change_presence(status=status, activity=activity)


async def set_presence(
    guild: discord.Guild, args: SetPresenceArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    if ctx is None or ctx.client is None or ctx.store is None:
        raise GuardError("presence control isn't available in this context")
    raw = await ctx.store.get_meta(PRESENCE_META_KEY)
    state: dict[str, Any] = json.loads(raw) if raw else {}
    if args.status is not None:
        state["status"] = args.status
    if args.activity is not None:
        if args.activity == "none":
            state["activity"] = state["text"] = None
        else:
            state["activity"], state["text"] = args.activity, args.text
    await apply_presence(ctx.client, state)
    await ctx.store.set_meta(PRESENCE_META_KEY, json.dumps(state))
    return {
        "status": state.get("status") or "online",
        "activity": state.get("activity"),
        "text": state.get("text"),
    }


async def set_nickname(
    guild: discord.Guild, args: SetNicknameArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    nick = args.nickname.strip() or None  # empty string → reset to the default name
    try:
        await guild.me.edit(nick=nick)
    except discord.Forbidden as exc:
        raise GuardError(
            "I need the 'Change Nickname' permission — grant it to my role (see deploy/README.md)"
        ) from exc
    return {"nickname": nick, "reset": nick is None}


async def server_stats(
    guild: discord.Guild, args: ServerStatsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    # Everything here is read from the cached guild object — no extra API calls, no member intent.
    age_days = (discord.utils.utcnow() - guild.created_at).days
    return {
        "name": guild.name,
        "members": guild.member_count,  # available without the members intent (from GUILD_CREATE)
        "channels": {
            "text": len(guild.text_channels),
            "voice": len(guild.voice_channels),
            "categories": len(guild.categories),
            "stage": len(getattr(guild, "stage_channels", [])),
            "forums": len(getattr(guild, "forums", [])),
        },
        "roles": max(len(guild.roles) - 1, 0),  # exclude @everyone
        "custom_emoji": len(guild.emojis),
        "boost_tier": guild.premium_tier,
        "boosts": guild.premium_subscription_count,
        "created": guild.created_at.date().isoformat(),
        "age_days": age_days,
    }


_MESSAGE_LINK_RE = re.compile(
    r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def _resolve_message(
    guild: discord.Guild, message: str, channel_query: str | None
) -> tuple[str, int]:
    """From a link, or a bare id plus a channel, return (channel name/id query, message id)."""
    link = _MESSAGE_LINK_RE.search(message.strip())
    if link:
        link_guild, channel_id, message_id = (int(part) for part in link.groups())
        if link_guild != guild.id:
            raise GuardError("that message link points at a different server")
        return str(channel_id), message_id
    ref = message.strip()
    if not ref.isdigit():
        raise GuardError("give me a message link, or a message id plus the channel")
    if channel_query is None:
        raise GuardError("a bare message id needs the channel too")
    return channel_query, int(ref)


def _resolve_emoji(guild: discord.Guild, raw: str) -> Any:
    """Return what ``add_reaction`` accepts: a guild Emoji/PartialEmoji, or a unicode string."""
    value = raw.strip()
    try:
        partial = discord.PartialEmoji.from_str(value)
    except Exception:  # malformed custom-emoji syntax — fall back to treating it as unicode
        partial = None
    if partial is not None and partial.id is not None:  # <:name:id> form
        return guild.get_emoji(partial.id) or partial
    if value.startswith(":") and value.endswith(":") and len(value) > 2:  # :name: form
        name = value.strip(":")
        for emoji in guild.emojis:
            if emoji.name == name:
                return emoji
        raise GuardError(f"no custom emoji named :{name}: in this server")
    return value  # a standard unicode emoji


async def add_reaction(
    guild: discord.Guild, args: AddReactionArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    channel_query, message_id = _resolve_message(guild, args.message, args.channel)
    channel, kind = _resolve_editable_channel(guild, channel_query)
    if kind != "text":
        raise GuardError("can only react to messages in a text channel")
    emoji = _resolve_emoji(guild, args.emoji)
    # A partial message skips a fetch — the react endpoint needs no message body, just the id.
    try:
        await channel.get_partial_message(message_id).add_reaction(emoji)
    except discord.NotFound as exc:
        raise GuardError("no message with that id in that channel") from exc
    except discord.Forbidden as exc:
        raise GuardError(
            "I need 'Add Reactions' and 'Read Message History' there (see deploy/README.md)"
        ) from exc
    except discord.HTTPException as exc:
        raise GuardError(f"Discord rejected that reaction: {exc.text or exc}") from exc
    return {
        "reacted": True,
        "channel": channel.name,
        "message_id": message_id,
        "emoji": str(emoji),
    }


# --------------------------------------------------------------------------- confirm preview


async def preview(name: str, guild: discord.Guild, args: Any) -> str:
    """Human-readable diff for a confirm-gated tool. Resolution errors surface before confirming."""
    if name == "set_permissions":
        channel, _ = _resolve_editable_channel(guild, args.channel)
        lines = [f"#{channel.name}:"]
        hides_from_everyone = False
        for overwrite in args.overwrites:
            target = await _resolve_target(guild, overwrite.target)
            tname = getattr(target, "name", str(target))
            allow = ", ".join(overwrite.allow) or "—"
            deny = ", ".join(overwrite.deny) or "—"
            lines.append(f"  {tname}: allow[{allow}] deny[{deny}]")
            if target == guild.default_role and "view_channel" in overwrite.deny:
                hides_from_everyone = True
        if hides_from_everyone:
            lines.append("  Roger: allow[view_channel, send_messages]  (kept — not locked out)")
        return "\n".join(lines)
    if name == "edit_channel":
        channel, kind = _resolve_editable_channel(guild, args.channel)
        lines = [f"#{channel.name}:"]
        if args.name is not None:
            new = sanitize_channel_name(args.name) if kind == "text" else sanitize_display_name(
                args.name
            )
            lines.append(f"  name: {channel.name} → {new}")
        if args.topic is not None:
            lines.append(f"  topic: {getattr(channel, 'topic', None) or '—'} → {args.topic}")
        if args.category is not None:
            _, cat_name = resolve_one(args.category, [(c.id, c.name) for c in guild.categories])
            current = getattr(channel, "category", None)
            lines.append(f"  category: {current.name if current else '—'} → {cat_name}")
        return "\n".join(lines)
    if name == "post_message":
        channel, _ = _resolve_editable_channel(guild, args.channel)
        body = args.content if len(args.content) <= 300 else args.content[:300] + "…"
        return f"post to #{channel.name}:\n{body}"
    if name == "move_channel":
        channel, kind = _resolve_editable_channel(guild, args.channel)
        label = "category" if kind == "category" else f"{kind} channel"
        if args.position is not None:
            where = f"to the {args.position} of its group"
        else:
            ref, _ = _resolve_editable_channel(
                guild, args.before if args.before is not None else args.after
            )
            rel = "before" if args.before is not None else "after"
            where = f"{rel} {ref.name}"
        return f"move {label} {channel.name} {where}"
    if name == "create_channel":
        head = f"create {args.kind} channel: {args.name}"
        if args.category:
            head += f" (under {args.category})"
        flags = []
        if args.private:
            flags.append("private — hidden from @everyone")
        if args.read_only:
            flags.append("read-only for @everyone")
        if flags:
            head += " — " + ", ".join(flags)
        lines = [head]
        for grant in args.grants:
            lines.append(f"  {grant.role}: allow[{', '.join(grant.allow)}]")
        return "\n".join(lines)
    return name


EXECUTORS = {
    "list_structure": list_structure,
    "create_channel": create_channel,
    "create_role": create_role,
    "set_permissions": set_permissions,
    "edit_channel": edit_channel,
    "post_message": post_message,
    "move_channel": move_channel,
    "run_digest": run_digest,
    "suggest_feeds": suggest_feeds,
    "add_feed": add_feed,
    "remove_feed": remove_feed,
    "list_feeds": list_feeds,
    "set_presence": set_presence,
    "set_nickname": set_nickname,
    "server_stats": server_stats,
    "add_reaction": add_reaction,
}
