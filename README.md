# roger

An [OpenRouter](https://openrouter.ai)-backed Discord bot — the spiritual successor to
[`roger-bot`](https://github.com/R055LE/roger-bot), my first-ever programming project. Same
character, rebuilt from scratch on hosted models and modern tooling.

Roger is a single-guild, owner-gated Discord assistant with three separate "brains":

- **Admin** — an owner-only server concierge. Ask in plain language ("a read-only podcast channel
  under Media that DJs can post in") and it creates channels/roles and sets permissions through a
  small, hand-rolled tool loop. No agent framework.
- **Ambient** — a deadpan chat persona for @mentions and DMs. No tools, no authority.
- **Digest** — a scheduled RSS/Atom summary posted to a channel.

## Security posture

Security is structural, not prompt-deep:

- **No privileged gateway intents.** `message_content`, `members`, and `presence` stay off. Roger
  only sees content in DMs, @mentions, and its own messages — exactly what it needs.
- **Owner-gated.** Admin actions require `user.id == OWNER_ID`, checked before a single token is
  spent. Everyone else gets a canned reply and an audit row.
- **Least privilege.** Never requests Administrator. Roles it creates always have zero permissions;
  access is granted through channel overwrites. No delete/rename/edit tools exist.
- **Budgeted.** Per-brain daily token caps and a hard cap on tool calls per request.
- **No secrets in git — ever, not even encrypted.** Secrets live in a `sops`+`age`-encrypted
  `roger.env` on the host; the repo carries only `.sops.yaml` and `roger.env.example`.

## Stack

Python 3.12 · [discord.py](https://github.com/Rapptz/discord.py) · the OpenAI SDK pointed at
OpenRouter · `pydantic` · `aiosqlite` · `feedparser`. Runs as a non-root, read-only-rootfs
container. `OPENROUTER_BASE_URL` is config, so pointing Roger at a local inference host later is an
env change, not a rewrite.

## Setup

```bash
# 1. Configure
cp roger.env.example roger.env        # fill in tokens, owner/guild IDs, model chains

# 2. Encrypt secrets at rest (age key generated once — see .sops.yaml)
sops -e -i roger.env

# 3. Run (secrets injected into the process env, never baked into the image)
mkdir -p data
sops exec-env roger.env 'docker compose up -d --build'
```

Each `MODEL_*` var is a comma-separated priority list (primary first, the rest are OpenRouter
fallbacks). Every model in `MODEL_ADMIN` must support tool calling.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Status

Feature-complete across the planned phases:

- **Admin** — owner-gated `/roger` and DMs; a hand-rolled tool loop with `list_structure`,
  `create_channel`, `create_role`, and confirm-gated `set_permissions`; per-request tool and daily
  token budgets; a full SQLite audit trail.
- **Ambient** — deadpan chat on @mentions and non-owner DMs, rate-limited per user + globally, with
  a short own-thread memory. No tools, ever.
- **Digest** — a scheduled daily RSS/Atom summary (also triggerable via `/roger run the digest
  now`), deduped so nothing posts twice.

Runs as a non-root, read-only-rootfs container. ~55 tests cover the guard rules, the tool loop, the
rate limiter, and the digest paths.

## License

MIT — see [LICENSE](LICENSE).
