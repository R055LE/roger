"""Executors: the actual Discord API calls behind each tool.

``snapshot`` doubles as the pre-request server state fed to the admin model and as the
``list_structure`` tool result (§7).
"""

from __future__ import annotations

from typing import Any

import discord

from roger.tools.schemas import ListStructureArgs


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


EXECUTORS = {"list_structure": list_structure}
