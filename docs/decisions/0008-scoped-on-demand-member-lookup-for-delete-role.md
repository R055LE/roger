# ADR-0008: A scoped, on-demand member lookup for `delete_role` — not the Members intent

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

ADR-0007 shipped `delete_role` without a membership check, because the obvious way to get one —
Discord's privileged **Members** intent — looked like an all-or-nothing trade: enable it and Roger
gains a standing, ambient cache of every guild member (joins, leaves, nickname/role changes, forever),
which cuts directly against §2.1's "Roger only ever sees content in DMs, @mentions, and its own
messages." That felt disproportionate to what it would buy: a better warning on one confirm dialog.

Before accepting that framing, two things were worth checking rather than assuming:

- **Is the intent actually all-or-nothing?** Checked Discord's own developer docs directly. Two
  relevant facts: (1) "HTTP API restrictions are independent of Gateway restrictions, and are
  unaffected by which intents your app passes in the `intents` parameter when Identifying" — the
  Developer Portal toggle and the gateway subscription are genuinely separate switches. (2) The
  toggle is still required, full stop, for *any* member-list access (REST or gateway) — there's no
  way to query role membership without it. So: not fully avoidable, but the ambient-cache
  consequence *is* avoidable — you can enable the portal toggle (unlocking REST calls like `Guild.
  fetch_members`) while never setting `Intents.members = True` on the actual connection, meaning no
  event stream and no automatic cache.
- **How does this get handled in practice?** discord.py's own docs: "Discord now further restricts
  the ability to cache members and expects bot authors to cache as little as is necessary,"
  recommending on-demand `Guild.query_members()` / `Guild.fetch_member()` over bulk caching — i.e.
  the platform's own recommended shape is exactly "fetch live, don't cache," not "subscribe to
  everything." Red-DiscordBot (a large, widely-self-hosted bot) was also checked as a reference
  point and turned out *not* to be one: it requests `discord.Intents.all()` unconditionally, because
  it's a general-purpose plugin framework that can't predict what a given cog will need and pushes
  the resulting risk onto the self-hosting operator ("it is *expected* that you know what's in your
  bot and how it works"). That's a different problem shape than Roger — one fixed tool surface, one
  operator — so it's not the applicable precedent; discord.py's own minimal-caching guidance is.

## Decision

Add `roger/tools/members.py`, a single module with one function, `role_holders(guild, role)`,
that makes an on-demand REST call and returns the current result — no caching, no gateway
subscription, nothing persisted. It is the only place in the codebase permitted to touch member
data; nothing else imports it. `delete_role`'s confirm preview (`executors.preview`) is the only
caller: it lists current holders when the lookup succeeds, and falls back to the original "Roger
cannot see who currently holds it" message on `discord.HTTPException` (the portal toggle is off).
Enabling the toggle is the operator's choice, documented in `deploy/README.md` as optional; when
off, behavior is unchanged from ADR-0007. §2.1's assertion (`_assert_non_privileged`) is untouched
and still passes — it checks what Roger's gateway connection requests, which stays `Intents.
default()` regardless of the portal toggle.

The membership list is informational only — it does not block deletion. The owner is still the one
deciding; this just gives them a real answer instead of a disclosed blind spot.

## Consequences

- `delete_role`'s confirm dialog can now show real membership, closing the gap ADR-0007 disclosed
  rather than solved.
- §2.1 is unchanged in the sense that matters (no ambient gateway subscription, no standing cache),
  but the Discord application itself now *can* be granted broader access if the operator opts in at
  the portal level — a capability boundary that exists outside Roger's own code and isn't something
  `_assert_non_privileged` can verify. The actual scoping guarantee is now: "only one function in
  this codebase calls it, and only one call site uses it" — enforced by code review and the module's
  own docstring, not by a runtime assertion the way the gateway intents are. That's a real, weaker
  guarantee than before, accepted deliberately rather than left implicit.
- No new config surface: the Discord portal toggle is the only switch. A Roger-level flag on top of
  it was considered and dropped as unneeded — start small, add one later if a second feature needs
  the same capability and the single-chokepoint discipline starts feeling thin.

## Amendment (2026-07-30): the original implementation didn't actually work

The Discord-API-level research above was correct — but `role_holders()` originally called `Guild.
fetch_members()`, discord.py's own convenience wrapper, and that wrapper has its *own* client-side
guard: `if not self._state._intents.members: raise ClientException(...)`. It checks Roger's local
`Intents` object (always `members=False`, §2.1) unconditionally — regardless of the portal toggle.
So the fallback path never actually triggered in production: the wrapper raised `ClientException`,
which isn't a `discord.HTTPException`, so callers' `except discord.HTTPException` never caught it,
and the owner saw a raw "Intents.members must be enabled" error no matter what they did in the
portal. Caught via live container logs after `list_role_members` shipped and the owner reported
Roger "still saying it needs intent" after enabling the toggle.

Fixed by going one layer lower: `role_holders()` now calls `guild._state.http.get_members(...)`
directly — the same REST call `fetch_members()` makes internally, minus its intents guard — and
works from the raw payload instead of constructing a real `discord.Member` (which needs a live
`ConnectionState` to build correctly; the raw payload has everything `role_holders()` actually
needs — role ids and a display name). The decision above is otherwise unchanged: still one function,
one call site now two (`list_role_members` also uses it), still on-demand, still discarded after use.
The added, real cost: `guild._state` and `.http` are private discord.py attributes, not part of its
public API, so a future discord.py upgrade could rename or restructure them without a deprecation
path — mitigated by the pinned version in `pyproject.toml` and a dedicated pagination test
(`test_role_holders_paginates`) that would fail loudly rather than silently degrade.
