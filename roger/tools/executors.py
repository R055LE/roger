"""Executors: the actual Discord API calls behind each tool.

``snapshot`` doubles as the pre-request server state fed to the admin model and as the
``list_structure`` tool result (§7). ``preview`` renders the exact change a confirm-gated tool
would make, so the owner approves against a real diff — not the model's paraphrase.
"""

from __future__ import annotations

from typing import Any

import discord

from roger.tools.guard import (
    GuardError,
    check_no_duplicate,
    parse_color,
    resolve_one,
    sanitize_channel_name,
    sanitize_display_name,
)
from roger.tools.schemas import (
    CreateChannelArgs,
    CreateRoleArgs,
    ListStructureArgs,
    RunDigestArgs,
    SetPermissionsArgs,
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


async def snapshot(guild: discord.Guild) -> dict[str, Any]:
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
        channels.append(
            {
                "id": channel.id,
                "name": channel.name,
                "kind": kind,
                "category": category,
                "topic": topic,
                "overwrites": _overwrite_summary(channel.overwrites),
            }
        )

    roles = [
        {"id": r.id, "name": r.name, "position": r.position, "color": str(r.color)}
        for r in guild.roles
    ]
    return {"categories": categories, "channels": channels, "roles": roles}


async def list_structure(guild: discord.Guild, args: ListStructureArgs) -> dict[str, Any]:
    return await snapshot(guild)


# --------------------------------------------------------------------------- resolution


def _resolve_channel(guild: discord.Guild, query: str) -> discord.abc.GuildChannel:
    items = [
        (c.id, c.name)
        for c in guild.channels
        if isinstance(c, (discord.TextChannel, discord.VoiceChannel))
    ]
    channel_id, _ = resolve_one(query, items)
    channel = guild.get_channel(channel_id)
    if channel is None:
        raise GuardError(f"channel {query!r} vanished")
    return channel


async def _resolve_target(guild: discord.Guild, query: str) -> discord.Role | discord.Member:
    q = query.strip()
    if q.casefold() in ("@everyone", "everyone"):
        return guild.default_role
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


async def create_channel(guild: discord.Guild, args: CreateChannelArgs) -> dict[str, Any]:
    if args.kind == "category":
        if args.category is not None:
            raise GuardError("a category can't be nested under another category")
        name = sanitize_display_name(args.name)
        check_no_duplicate("category", name, [c.name for c in guild.categories])
        created = await guild.create_category(name=name)
        return {"created": "category", "id": created.id, "name": created.name}

    category_obj = None
    if args.category is not None:
        cat_id, _ = resolve_one(args.category, [(c.id, c.name) for c in guild.categories])
        category_obj = guild.get_channel(cat_id)

    if args.kind == "text":
        name = sanitize_channel_name(args.name)
        check_no_duplicate("text channel", name, [c.name for c in guild.text_channels])
        overwrites: dict[Any, discord.PermissionOverwrite] = {}
        if args.read_only:
            # New channel, blast radius nil — applied at creation without a confirm (§2.8).
            overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
        created = await guild.create_text_channel(
            name=name, category=category_obj, topic=args.topic, overwrites=overwrites
        )
        return {
            "created": "text",
            "id": created.id,
            "name": created.name,
            "category": category_obj.name if category_obj else None,
            "read_only": args.read_only,
        }

    name = sanitize_display_name(args.name)
    check_no_duplicate("voice channel", name, [c.name for c in guild.voice_channels])
    created = await guild.create_voice_channel(name=name, category=category_obj)
    return {
        "created": "voice",
        "id": created.id,
        "name": created.name,
        "category": category_obj.name if category_obj else None,
    }


async def create_role(guild: discord.Guild, args: CreateRoleArgs) -> dict[str, Any]:
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


async def set_permissions(guild: discord.Guild, args: SetPermissionsArgs) -> dict[str, Any]:
    channel = _resolve_channel(guild, args.channel)
    applied: list[dict[str, Any]] = []
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
    return {"channel": channel.name, "applied": applied}


async def run_digest(guild: discord.Guild, args: RunDigestArgs) -> dict[str, Any]:
    # Stub until P5 wires the digest brain.
    return {"status": "run_digest is registered; the digest job lands in P5"}


# --------------------------------------------------------------------------- confirm preview


async def preview(name: str, guild: discord.Guild, args: Any) -> str:
    """Human-readable diff for a confirm-gated tool. Resolution errors surface before confirming."""
    if name == "set_permissions":
        channel = _resolve_channel(guild, args.channel)
        lines = [f"#{channel.name}:"]
        for overwrite in args.overwrites:
            target = await _resolve_target(guild, overwrite.target)
            tname = getattr(target, "name", str(target))
            allow = ", ".join(overwrite.allow) or "—"
            deny = ", ".join(overwrite.deny) or "—"
            lines.append(f"  {tname}: allow[{allow}] deny[{deny}]")
        return "\n".join(lines)
    return name


EXECUTORS = {
    "list_structure": list_structure,
    "create_channel": create_channel,
    "create_role": create_role,
    "set_permissions": set_permissions,
    "run_digest": run_digest,
}
