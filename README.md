# roger

An [OpenRouter](https://openrouter.ai)-backed Discord bot — the spiritual successor to
[`roger-bot`](https://github.com/R055LE/roger-bot), my first-ever programming project. Same
character, rebuilt from scratch on hosted models and modern tooling.

Roger is a single-guild, owner-gated Discord assistant with three separate "brains":

- **Admin** — an owner-only server concierge, reachable by `/roger`, a DM, or an @mention. Ask in
  plain language ("a read-only podcast channel under Media that DJs can post in") and it creates
  channels/roles and sets permissions through a small, hand-rolled tool loop, with short
  per-channel conversation memory so follow-ups have context. No agent framework.
- **Ambient** — a deadpan chat persona (via `/chat`, or any non-owner @mention/DM). No tools, no
  authority.
- **Digest** — a scheduled RSS/Atom summary posted to a channel.

Design details — the routing table, the tool loop, the store schema, and the security invariants the
source cites as `(§N)` — are in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Security posture

Security is structural, not prompt-deep:

- **No privileged gateway intents.** `message_content`, `members`, and `presence` stay off. Roger
  only sees content in DMs, @mentions, and its own messages — exactly what it needs.
- **Owner-gated.** Admin actions require `user.id == OWNER_ID`, checked before a single token is
  spent. Everyone else gets a canned reply and an audit row.
- **Least privilege.** Never requests Administrator. Roles it creates always have zero permissions;
  access is granted through channel overwrites. No delete, kick, ban, or purge tools exist — Roger
  creates and adjusts, never destroys, and every change to existing state is owner-confirmed.
- **Budgeted.** Per-brain daily token caps and a hard cap on tool calls per request.
- **No secrets in git — ever, not even encrypted.** Secrets live in a `sops`+`age`-encrypted
  `roger.env` on the host; the repo carries only `.sops.yaml` and `roger.env.example`.

## Stack

Python 3.12 · [discord.py](https://github.com/Rapptz/discord.py) · the OpenAI SDK pointed at
OpenRouter · `pydantic` · `aiosqlite` · `feedparser`. Runs as a non-root, read-only-rootfs
container. `OPENROUTER_BASE_URL` is config, so pointing Roger at a local inference host later is an
env change, not a rewrite.

## Configure

```bash
cp roger.env.example roger.env        # fill in tokens, owner/guild IDs, model chains
```

Each `MODEL_*` var is a comma-separated priority list (primary first, the rest are OpenRouter
fallbacks). Every model in `MODEL_ADMIN` must support tool calling.

## Deploy

CI/CD is pull-based: pushing to `main` runs the tests, builds the image, and publishes it to
GHCR (`ghcr.io/r055le/roger:main`); the host polls that tag and redeploys itself. Secrets are
injected at runtime by `sops exec-env` and never baked into the image. Full runbook (host
bootstrap, age key, systemd timer) in [`deploy/`](deploy/README.md).

Run it directly on any Docker host:

```bash
sops -e -i roger.env                                  # encrypt at rest (age key — see .sops.yaml)
mkdir -p data
sops exec-env roger.env 'docker compose up -d'        # pulls the published image
```

## Observability

Roger exposes Prometheus metrics on `:${METRICS_PORT}/metrics` (default `9108`; set `METRICS_PORT=0`
to disable). Event counters — LLM requests, errors, and budget rejections — are incremented in
process; state gauges — token/dollar spend, caps, feed count, and audit tallies — are refreshed from
SQLite so they survive restarts. Key series:

| Metric | Type | Labels |
|---|---|---|
| `roger_tokens_today` / `roger_tokens_cap` | gauge | `brain` |
| `roger_cost_usd_today` | gauge | `brain` |
| `roger_llm_requests_total` / `roger_llm_errors_total` | counter | `brain` (`type`) |
| `roger_llm_budget_exceeded_total` | counter | `brain` |
| `roger_audit_events` | gauge | `tool`, `status` |
| `roger_feeds`, `roger_build_info` | gauge | — (`version`) |

A ready-to-merge Prometheus scrape job and an importable Grafana dashboard live in
[`deploy/observability/`](deploy/observability/) — the bridge to the
[`sre-observability-lab`](https://github.com/R055LE/sre-observability-lab).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Status

Feature-complete across the planned phases:

- **Admin** — owner-gated via `/roger`, DM, or @mention, with short per-channel conversation
  memory; a hand-rolled tool loop with `list_structure`, `create_channel`, `create_role`,
  confirm-gated `set_permissions` / `edit_channel` / `post_message` / `move_channel`, and feed
  curation (`suggest_feeds`, `add_feed`, `remove_feed`, `list_feeds`); per-request tool and daily
  token budgets; a full SQLite audit trail.
- **Ambient** — deadpan chat via `/chat` or any non-owner @mention/DM, rate-limited per user +
  globally, with a short own-thread memory. No tools, ever.
- **Digest** — a scheduled daily RSS/Atom summary (also triggerable via `/roger run the digest
  now`), deduped so nothing posts twice. Roger curates its own feed list: `DIGEST_FEEDS` seeds it
  once, then Roger validates candidates against the live web and adds or drops them on request.

Runs as a non-root, read-only-rootfs container. ~110 tests cover the guard rules, the tool loop
(including channel creation with access presets and the confirm-gated edit, post, and reorder
tools), the rate limiter, and the digest and feed-curation paths.

## License

MIT — see [LICENSE](LICENSE).
