# roger

An [OpenRouter](https://openrouter.ai)-backed Discord bot — the spiritual successor to
[`roger-bot`](https://github.com/R055LE/roger-bot), my first-ever programming project. Same
character, rebuilt from scratch on hosted models and modern tooling.

Roger is a single-guild, owner-gated Discord assistant with four separate "brains":

- **Admin** — an owner-only server concierge, reached through `/roger` in the guild. Ask in
  plain language ("a read-only podcast channel under Media that DJs can post in") and it creates
  channels/roles and sets permissions through a small, hand-rolled tool loop, with short
  per-channel conversation memory so follow-ups have context. No agent framework.
- **Ambient** — a deadpan chat persona (via `/chat`, any @mention, or any DM). No tools, no
  authority.
- **Curated** — a daily check of [Scout](https://github.com/R055LE/scout)'s picks. It posts one
  source-grounded story when there is something worth sharing, and skips quiet days. Public posting
  stays off until `CURATED_CHANNEL_ID` is set. The owner can preview a draft through `/roger`.
- **Giga Brain** — an owner-only, read-only strategic-analysis mode, reachable by `/gigabrain`. It
  reviews live server state and reasons about it — never acts. Optionally also checks in on its
  own on a configurable interval, DMing the owner unprompted suggestions.

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
- **Untrusted item output is bounded.** Curated drafts use bounded Scout article evidence, require
  supporting excerpts, accept only HTTP(S) item links, and suppress Discord mentions on delivery.
- **No secrets in git — ever, not even encrypted.** Secrets live in a `sops`+`age`-encrypted
  `roger.env` on the host; the repo carries only `.sops.yaml` and `roger.env.example`.
- **Signed, scanned supply chain.** CI audits deps (`pip-audit`) and scans the image (Trivy, build →
  scan → push); releases are signed keyless with cosign and the deploy host verifies the signature
  before it runs — a bad or unsigned image fails the deploy closed.

## Stack

Python 3.12 · [discord.py](https://github.com/Rapptz/discord.py) · the OpenAI SDK pointed at
OpenRouter · `pydantic` · `aiosqlite`. Runs as a non-root, read-only-rootfs
container. `OPENROUTER_BASE_URL` is config, so pointing Roger at a local inference host is an env
change rather than a rewrite. That was tested and shelved on cost and hardware grounds, so hosted is
the only supported setup today: see
[ADR-0009](docs/decisions/0009-hosted-inference-over-local-serving.md).

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

Roger exposes Prometheus metrics on port `9108` inside the container by default; set
`METRICS_PORT=0` to disable the server. Compose publishes it as
`127.0.0.1:${METRICS_HOST_PORT:-9108}` by default. Set `METRICS_BIND_ADDRESS` only when a scraper on a
trusted private interface needs remote access. The endpoint has no authentication or TLS and must
not be exposed to an untrusted network.

Event counters — LLM requests, errors, and budget rejections — are incremented in process; state
gauges — token/dollar spend, caps, and audit tallies — are refreshed from SQLite so they
survive restarts. Key series:

| Metric | Type | Labels |
|---|---|---|
| `roger_tokens_today` / `roger_tokens_cap` | gauge | `brain` |
| `roger_cost_usd_today` / `roger_cost_usd_cap` | gauge | `brain` |
| `roger_llm_requests_total` / `roger_llm_errors_total` | counter | `brain` (`type`) |
| `roger_llm_budget_exceeded_total` | counter | `brain`, `reason` |
| `roger_audit_events` | gauge | `tool`, `status` |
| `roger_build_info` | gauge | — (`version`) |

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

- **Admin** — owner-gated via `/roger` in the guild, with short per-channel conversation
  memory; a hand-rolled tool loop with `list_structure`, `create_channel`, `create_role`, and
  confirm-gated `set_permissions` / `edit_channel` / `post_message` / `move_channel`; per-request
  tool and daily token budgets; a full SQLite audit trail.
- **Ambient** — deadpan chat via `/chat`, any @mention, or any DM, rate-limited per user +
  globally, with a short own-thread memory. No tools, ever.
- **Curated** — defaults to checking Scout's output at 07:00 local time and may post one useful
  story with source detail and a light take. Set `CURATED_CHECK_INTERVAL_MINUTES` to enable an
  ongoing stream, bounded by `CURATED_MAX_POSTS_PER_DAY`, `CURATED_MIN_SPACING_MINUTES`,
  `CURATED_MAX_OBSERVATIONS_PER_DAY`, and the existing model budgets. There is no posting quota.
  Quiet or rejected input is remembered; changed evidence or model policy allows reconsideration.
  Transient failures get one retry after an hour; budget deferrals get one on the next local day.
  `/status` shows remaining allowances, spacing, uncertain deliveries, and the last decisions.
  The owner can preview the next decision with
  `/roger preview the curated post`; that spends from the curated model budget but does not post or
  change Curated state. Ask for `preview_curated` with `developing_stories=true` to inspect the
  bounded story-memory path. It follows confirmed sent reports across source updates, records
  explicit publish/update/combine/hold/skip decisions, and stays disabled until
  `CURATED_DEVELOPING_STORIES=true`. With the default `CURATED_REQUEST_PROFILE=legacy`, story
  drafts and the one allowed revision have a fixed 4,096
  token output ceiling; legacy Curated drafts and reviews keep the 900-token ceiling, and every
  call spends from the same Curated daily token and dollar budgets. Set `CURATED_CHANNEL_ID` only
  after reviewing preview quality. See
  [ADR-0012](docs/decisions/0012-scout-is-the-item-source.md) for the Scout boundary. Scout currently
  fetches only newly reported items; [Scout #21](https://github.com/R055LE/scout/issues/21) tracks
  same-page refresh evidence.

  `CURATED_REQUEST_PROFILE=sol-astra-v1` requires developing stories and selects the evaluated
  Sol 20260929 writer (16,384 output tokens, 1,200-second timeout) and Astra 20260903 reviewer
  (8,192 output tokens, 900-second timeout). Both use OpenAI, default service tier, medium
  reasoning, no temperature, and one transport attempt with no provider fallback. Response
  caching is disabled; prompt caching uses explicit mode with no cache markers. Unknown cache
  usage stays unknown. Reported cache activity, a mismatched route, or an unusable response
  prevents publication; trustworthy usage is recorded even on rejected responses. Missing usage
  and transport failures are recorded as uncertain, without retaining response text. Selected
  profiles reject legacy Curated calls and do not automatically replay uncertain calls.

  The scheduled job keeps its 30-minute overall lease and deadline, which can interrupt the
  four-call workflow before all per-call timeouts elapse. The selector defaults to `legacy` and
  does not change deployed channels, schedules, or budgets. Selecting the profile also requires
  positive `DAILY_USD_CURATED`, `DAILY_ATTEMPTS_CURATED`, and `DAILY_WORKFLOWS_CURATED`; all three
  default to zero. One workflow is a scheduled editorial evaluation or manual preview, with up to
  four model attempts. These limits are independent of source observations and posted messages.

  Each selected-profile attempt reserves its request-specific input bound and full output ceiling
  before provider I/O. The input bound includes UTF-8 messages, the complete schema, and framing,
  up to 32,768 tokens; no evidence is trimmed to fit the budget. The dollar reservation includes
  the higher cache-write input price. Admission compares used plus outstanding amounts against
  both daily caps, including legacy Curated usage. Trustworthy completed usage settles once;
  malformed, interrupted, or ambiguous calls keep their reservation. Reported overruns retain the
  larger amount and hold all further selected-profile admissions: the previous bound was wrong.
  Higher caps, midnight, and restarts do not release that hold; explicit reconciliation is required.
  Reservations never expire or automatically replay.

  Budget days use the host-local day, as existing usage does; posting and source-observation limits
  use `TZ`. Attempts count on admission day and actual settled charges on completion day. A workflow
  continued after midnight counts on each day it makes a call. Unresolved reservations carry across
  midnight and restarts until explicitly reconciled; there is no automatic release or reconciliation
  command. `/status` shows used, reserved, and unknown amounts plus the last model or budget refusal.
  The content-free accounting ledger follows existing usage retention and is not automatically
  pruned. Legacy model profiles keep their existing budget behavior.

Runs as a non-root, read-only-rootfs container. More than 300 tests cover the guard rules, the tool loop
(including channel creation with access presets and the confirm-gated edit, post, and reorder
tools), the rate limiter, and the Scout ingestion path.

## License

MIT — see [LICENSE](LICENSE).
