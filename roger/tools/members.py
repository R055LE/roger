"""On-demand, uncached member lookups — the one place in the codebase allowed to touch member data.

Requires the **Members** privileged intent enabled for the Discord application (portal toggle),
which is independent of Roger's gateway subscription: `Intents.default()` (§2.1) keeps
`members=False` on the connection, so this never turns into an ambient GUILD_MEMBER_ADD/UPDATE/
REMOVE stream or a standing cache — every call hits the REST API fresh and the result is discarded
after use, per Discord's own guidance to "cache as little as is necessary." If the portal toggle is
off, the fetch fails with `discord.HTTPException` and callers fall back gracefully (see
`executors.preview`'s `delete_role` branch) — the feature is opt-in and fails safe. See ADR-0008.
"""

from __future__ import annotations

import discord


async def role_holders(guild: discord.Guild, role: discord.Role) -> list[discord.Member]:
    """Members currently holding ``role``, fetched live. Raises ``discord.HTTPException`` if the
    Members intent isn't enabled for the application — callers must catch that and degrade
    gracefully rather than surface a raw API error to the model or owner."""
    return [member async for member in guild.fetch_members(limit=None) if role in member.roles]
