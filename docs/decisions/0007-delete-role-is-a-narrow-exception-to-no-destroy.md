# ADR-0007: `delete_role` is a narrow, deliberate exception to "no destroy"

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

§2.5 was written as a blanket rule: Roger has no delete, kick, ban, or bulk-purge tool anywhere in
the surface, so worst case — a confused or hallucinating model — nothing it does is unrecoverable.
That's the property that lets a hand-rolled tool loop run with no framework and still be trusted.

The gap surfaced in practice: asked to clean up a role, Roger had no way to do it — the closest
existing tool, `create_role`, only adds. The owner then has to do the actual cleanup step by hand in
Discord anyway, which is exactly the friction an admin bot is supposed to remove.

Two things push back on applying the blanket rule to roles specifically:

1. **Confirm-gating, not tool non-existence, is what actually prevents mistakes here.** Every other
   mutation (`set_permissions`, `edit_channel`) already trusts "the model proposes, the owner sees a
   rendered diff and approves" as sufficient protection against a hallucinating model — including for
   irreversible-in-effect changes like hiding a channel from @everyone. An owner who explicitly
   confirms "yes, delete this role" isn't meaningfully safer doing the same thing by hand in Discord.
2. **Roles here are already deliberately inert.** Always zero permissions (§2.6), no messages, no
   history, no content. Deleting one loses far less than deleting a channel or removing a member
   would.

A real limitation shaped the design: Roger runs without the privileged Members gateway intent
(§2.1), which is a much larger privacy invariant than this feature and was never in question. Without
it, `role.members` is an unreliable, partial cache — not a real roster — so an automated "refuse if
any member holds this role" guard was considered and dropped: it would silently under-report and
create false confidence, which is worse than no check at all.

## Decision

Add `delete_role`, confirm-gated like every other mutating tool. It refuses `@everyone` and
Discord-managed roles (bot/integration/booster — Discord rejects these edits anyway). It does **not**
attempt a membership check; the confirm preview says plainly that Roger cannot see who currently
holds the role, so the owner knows to check Discord themselves if that matters. Also added
`edit_role` (rename/color/hoist/mentionable) alongside it — a fully reversible "adjust existing
state" tool with no invariant conflict, following the same shape as `edit_channel`.

Everything else stays under the blanket rule: no channel delete, no kick/ban, no bulk-purge. This is
a role-shaped exception, not a general loosening of §2.5.

## Consequences

- Roger can now finish a "clean up this role" request end to end instead of stopping short.
- §2.5 is no longer a strict "nothing destroys anything" invariant — it's "nothing destroys anything
  **except a confirmed role deletion**." Any future exception needs its own ADR and its own
  case for why confirm-gating is enough; this one doesn't set a precedent that content-bearing
  deletes (channels, messages, members) are next.
- The membership-safety story for `delete_role` rests entirely on the owner reading the confirm
  diff — there is no automated backstop, and that's a real, disclosed limitation, not a hidden one.
