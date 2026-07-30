"""On-demand, uncached member lookups — the one place in the codebase allowed to touch member data.

Requires the **Members** privileged intent enabled for the Discord application (portal toggle),
which is independent of Roger's gateway subscription: `Intents.default()` (§2.1) keeps
`members=False` on the connection, so this never turns into an ambient GUILD_MEMBER_ADD/UPDATE/
REMOVE stream or a standing cache — every call hits the REST API fresh and the result is discarded
after use, per Discord's own guidance to "cache as little as is necessary." If the portal toggle is
off, the fetch fails with `discord.HTTPException` and callers fall back gracefully (see
`executors.preview`'s `delete_role` branch) — the feature is opt-in and fails safe. See ADR-0008.

Deliberately calls the REST endpoint directly instead of ``Guild.fetch_members()``: discord.py's
wrapper refuses to run unless its *own* ``Intents`` object has ``members=True``, regardless of the
portal toggle — a client-side guard stricter than what Discord's API actually requires ("HTTP API
restrictions are independent of Gateway restrictions, and are unaffected by which intents your app
passes... when Identifying," per Discord's docs). Working from the raw payload also sidesteps
constructing a real ``discord.Member`` (which needs a live ``ConnectionState``, not just a guild) —
the only field ever used downstream is the display name, so that's all this returns. The one real
cost: `guild._state`/`.http` are private discord.py attributes (ADR-0008 amendment, 2026-07-30).
"""

from __future__ import annotations

from typing import Any

import discord

_PAGE_SIZE = 1000  # Discord's max per page for GET /guilds/{id}/members


def _display_name(raw: dict[str, Any]) -> str:
    """Mirrors ``discord.Member.display_name``: nick, else global name, else username."""
    user = raw["user"]
    return raw.get("nick") or user.get("global_name") or user["username"]


async def role_holders(guild: discord.Guild, role: discord.Role) -> list[str]:
    """Display names of members currently holding ``role``, fetched live. Raises
    ``discord.HTTPException`` if the Members intent isn't enabled for the application — callers
    must catch that and degrade gracefully rather than surface a raw API error."""
    state = guild._state  # private attribute, deliberately — see module docstring
    names: list[str] = []
    after: int | None = None
    while True:
        page = await state.http.get_members(guild.id, _PAGE_SIZE, after)
        if not page:
            return names
        for raw in page:
            if role.id in {int(r) for r in raw["roles"]}:
                names.append(_display_name(raw))
        if len(page) < _PAGE_SIZE:
            return names
        after = int(page[-1]["user"]["id"])
