# AGENTS.md: roger

A single-guild, owner-gated Discord assistant on OpenRouter. One process, four
independent brains chosen by who is talking and where: Admin (owner, has tools),
Ambient (anyone, no tools), Digest (scheduled), Giga Brain (owner, read-only).

No agent framework. The admin brain is a hand-rolled tool loop so every step is
inspectable and bounded.

## Read first

- **`ARCHITECTURE.md`** describes the shape. Source comments cite it as `(§N)`.
  It is a design reference, not a spec: **where it and the code disagree, the
  code wins.**
- **`docs/decisions/`** holds short ADRs for the choices that had real
  tradeoffs. Read the one covering what you're about to change.
- **`BACKLOG.md`** for deferred work.

## Safety is structural, not prompted

This is the load-bearing idea in the whole codebase. The security invariants in
`ARCHITECTURE.md` §2 hold **regardless of what any model outputs**, because they
are enforced by which gateway intents are off, which tools exist, and which
permissions are expressible.

Consequences for anything you change:

- **Don't weaken an invariant to make a feature easier.** If a feature needs
  `message_content`, `members` or `presences` on the gateway, that is a decision
  with an ADR, not a config tweak. `_assert_non_privileged` fails at startup on
  purpose.
- **Don't move a guarantee into the prompt.** "The model is instructed not to"
  is not an invariant. If the tool exists and the caller is authorised, assume
  it will eventually be called.
- Giga Brain is **read-only**. It proposes and never acts. Adding a mutating
  tool to its subset changes what it is.
- Commands are guild-scoped to `GUILD_ID`. Single-guild is a scope decision, not
  an unfinished feature.

## Secrets

`.sops.yaml` is here for a reason. Nothing unencrypted, ever: no token, no
OpenRouter key, no guild or owner ID in a committed file. `roger.env.example`
shows shape only. If you find a real value committed, **stop and say so** so it
can be rotated, rather than deleting it and moving on.

## Deploying

`compose.yaml` is committed and hermes pulls images from GHCR on a systemd
timer, so the image half reconciles itself. The compose file half does not: the
deployed `/opt/roger/compose.yaml` got there by hand. A drift check for that
lives in the homelab repo.

Docs-only changes should not trigger a rebuild. That exclusion already exists in
the workflow; keep it working.

## Claude Code specifics

`CLAUDE.md` is a symlink to this file. Codex reads only `AGENTS.md`, Claude Code
reads only `CLAUDE.md`, and neither reads the other's, so one file serves both.
`/init` will try to replace the symlink with a real file.
