# Architecture

How Roger is put together and why. Source comments cite these sections as `(§N)` — this file is
what they point to. It's a design reference, not a spec to implement against; the code is the
source of truth, and where they ever disagree, the code wins.

Decisions that had real tradeoffs — the *why* behind a choice, and what it cost — are recorded as
short ADRs in [`docs/decisions/`](docs/decisions/). This file describes the shape; those explain the
turns.

## §1 Overview

Roger is a **single-guild, owner-gated Discord assistant** built on hosted models via OpenRouter.
It runs as one process with four independent **brains**, chosen entirely by *who* is talking and
*where*:

| Brain | Purpose | Tools | Who |
|---|---|---|---|
| **Admin** (§6) | Server concierge — creates channels/roles, sets permissions | Yes | Owner only |
| **Ambient** (§8) | Deadpan chat persona | None | Anyone |
| **Curated** (§9) | Source-grounded story selection and optional public post | None | n/a |
| **Giga Brain** (§12) | Deep, occasional strategic analysis — reviews server state, proposes ideas, never acts | Read-only subset | Owner only |

No agent framework. The admin brain is a hand-rolled tool loop (§6) so every step is inspectable
and bounded. The design goal is that **safety is structural** — enforced by which intents are off,
which tools exist, and which permissions are expressible — not by prompt wording (§2).

## §2 Security invariants

These hold regardless of what any model outputs. They are the load-bearing part of the design.

- **§2.1 No privileged gateway intents.** The client uses `Intents.default()` — `message_content`,
  `members`, and `presences` stay **off** on the gateway connection itself, asserted at startup
  (`_assert_non_privileged`). Roger only ever sees content in DMs, @mentions, and its own messages,
  and never gets an ambient member cache or member-change event stream. Discord's privileged-intent
  gate on REST endpoints is a *separate*, independent toggle (an app-level Developer Portal switch,
  not something `Intents.default()` touches) — `roger/tools/members.py` uses it for one narrow,
  on-demand, uncached lookup; see its module docstring and ADR-0008 for why that doesn't weaken this
  invariant.
- **§2.2 Single-guild scope.** Commands are registered guild-scoped to `GUILD_ID`; the bot serves
  exactly one server and ignores everything else.
- **§2.3 Owner gate before spend.** Admin actions require `user.id == OWNER_ID`, checked *before*
  any LLM dispatch — a non-owner costs zero tokens and gets a canned reply plus an audit row.
- **§2.4 Least privilege.** Roger never requests Administrator. The bot is invited with exactly the
  scopes its tools need — View Channels, Manage Channels, Manage Roles, Send Messages, Embed Links
  (the invite permission integer and the reasoning are in [`deploy/`](deploy/README.md)). `Manage
  Roles` is broad at the Discord layer, but the *expressible* actions are bounded at the tool layer:
  roles are always created with zero permissions (§2.6) and channel overwrites are drawn from a
  fixed allowlist (§2.7), so the gateway permission is far wider than anything Roger can actually do.
- **§2.5 No destructive or escalating tools, except one narrow, deliberate exception.** Nothing else
  Roger can do is irreversible: there is no kick, ban, bulk-purge, or channel-delete tool anywhere
  in the surface. Roger *creates*, and it *adjusts* existing state — renaming a channel, editing a
  topic, moving it under a category, reordering channels and categories, setting channel overwrites,
  posting a message — but every adjustment is reversible and confirm-gated (§2.8). The blast radius
  is bounded by what simply doesn't exist: almost no tool destroys anything. `delete_role` is the one
  exception (ADR-0007): roles are already deliberately powerless (zero permissions, §2.6, no
  messages or history), and the real safety mechanism for every mutation here is confirm-gating, not
  "the tool doesn't exist" — an owner who explicitly approves a rendered diff isn't meaningfully
  safer typing the same action into Discord's own UI. Content-bearing objects (channels, messages,
  members) keep the blanket rule; a bare, historyless role does not. Its confirm preview optionally
  names current holders via the on-demand lookup in §2.1/ADR-0008 — informational only, never a
  block, so the owner keeps final say even when a role is still assigned to someone.
- **§2.6 Roles are created with zero permissions.** `create_role` always passes
  `Permissions.none()`; access is granted through channel overwrites, never role permissions.
- **§2.7 Permission allowlist.** Only a fixed set of overwrite bits is expressible through the
  tool schema (§7). Anything outside the allowlist is *unrepresentable* — the model literally
  cannot ask for it.
- **§2.8 Confirm-gated mutations.** Every tool that changes *existing* state — `set_permissions`,
  `edit_channel`, `post_message`, `move_channel` — requires interactive owner approval against a
  rendered diff before it runs. Creation is exempt by default: `create_channel` / `create_role` add new, empty,
  zero-permission objects, and setting a brand-new channel or category's access at creation (`read_only`,
  per-role `grants`) has nil blast radius — no members, no history — so it applies immediately.
  Whenever an overwrite restricts @everyone — View Channel for private creation, Send Messages
  for read-only creation, or later via
  `set_permissions` — Roger grants its tagged dedicated bot role the destination's complete
  delivery capability set (or the normal view/send management minimum), so it can never lock
  itself out of a space it manages (@everyone includes the bot); the complete replacement and
  cleared bits show up in the confirm diff. The
  **one deliberate exception is
  `create_channel(private=True)`**: hiding a channel is still nil-blast-radius, but it is
  confirm-gated anyway — a hidden channel is a surprising side effect, and keeping the confirm ritual
  consistent for security-relevant actions is worth more than shaving a click. Confirmation can thus
  be *static* (a tool always confirms) or *conditional on the args* (`create_channel` confirms only
  when `private`), via `ToolSpec.needs_confirm`.
- **§2.9 Budgets.** A hard cap of **10 tool calls per request** (`ADMIN_MAX_TOOL_CALLS`) and **14
  model round-trips** (`ADMIN_MAX_TURNS`), plus per-brain **daily token caps**, optionally layered
  with a **daily USD cap** (§11), checked before every call. All caps are env-overridable per
  deployment; hitting the tool-call cap mid-request logs a warning and posts once to the ops channel
  (if configured), and the model is told to say so plainly — the cap resets on the next request, not
  on a timer.
- **§2.10 No secrets in git — ever, not even encrypted.** Secrets live only in a `sops`+`age`
  encrypted `roger.env` on the host. The repo carries `.sops.yaml` (the public recipient) and
  `roger.env.example`. See [`deploy/`](deploy/README.md).

## §3 Configuration

All settings load from the process environment via `pydantic-settings` (`roger/config.py`), injected
at runtime by `sops exec-env`. Nothing is read from a committed file. Notable shapes:

- `MODEL_ADMIN` / `MODEL_AMBIENT` / `MODEL_CURATED` / `MODEL_GIGABRAIN` are **comma-separated priority
  chains**. The primary comes first, followed by OpenRouter fallbacks. Every model in the admin
  chain must support tool calling.
- `OPENROUTER_BASE_URL` is config, so pointing Roger at a local inference host is an env change.
- `SCOUT_DIGEST_DIR` / `SCOUT_MAX_AGE_HOURS` point at Scout's read-only digest mount (§9).

## §4 Runtime & process model

One `asyncio` process (`python -m roger`). Non-root, read-only root filesystem, `/tmp` on tmpfs,
one writable bind mount at `/data` for the SQLite DB. Structured JSON logs to stdout
(`_JsonFormatter`); discord.py's gateway chatter is pinned to WARNING. `discord.py`'s
`ext.tasks` drives the optional daily or interval curated loop (§9).

## §5 Dispatch & routing

`classify_message` is a **pure** function (no side effects, unit-tested with fakes) that maps an
incoming message to a `Route`. Because `message_content` is off (§2.1), guild messages that neither
mention Roger nor arrive in a DM show up with empty content and are ignored by design.

| Condition | Route | Brain |
|---|---|---|
| Author is Roger | `IGNORE` | — |
| Empty content | `IGNORE` | — |
| DM from anyone | `AMBIENT_DM` | Ambient |
| Guild @mention from anyone | `AMBIENT_MENTION` | Ambient |
| Guild, no mention | `IGNORE` | — |

Slash commands bypass classification: `/roger <request>` → admin (owner-gated, guild only),
`/chat <message>` → ambient (open to anyone). An owner DM or @mention cannot invoke admin tools.
On the mention route the leading mention is stripped first so the model sees a clean request; an
empty remainder is dropped.

## §6 Admin brain — the tool loop

`handle_admin_request` runs a bounded loop, decoupled from Discord so it stays testable:

1. Log the request to the audit trail, then build the message list: a system prompt, the current
   **server snapshot** (§7) as JSON, short **per-channel conversation memory** (owner follow-ups
   like "rename it" have context — only request/answer text is kept, never tool machinery), and the
   new request.
2. Call the model with the tool schemas. If it returns plain text, that's the answer — persist the
   turn and return.
3. If it returns tool calls: validate each against its pydantic model, run guard rules, then either
   execute or (for confirm-gated tools) pause for owner approval against a rendered diff. Feed each
   result back as a `tool` message and loop.

Every outcome — ok, denied, invalid args, guard rejection, executor error, budget exhaustion — is
recorded to `audit` and surfaced to the model as a **structured result**, never a raised exception.
Bounds: 10 tool calls, 14 turns (§2.9).

## §7 Tools — schemas, guards, executors

Three layers, one per file under `roger/tools/`:

- **`schemas.py`** — each tool is a `ToolSpec`: name, description, a pydantic args model
  (`extra="forbid"`, so the model can't smuggle fields), and a confirm rule — static
  (`requires_confirm`) or per-call (`confirm_when(args)`, e.g. `create_channel` confirms only when
  `private=True`), evaluated through `needs_confirm`. `openai_tools()` renders the registry into the
  function-calling schema the model sees. The permission **allowlist** (§2.7) is a `Literal` of ten
  overwrite bits — nothing else is expressible, at creation (`grants`) or after (`set_permissions`).
- **`guard.py`** — pure sanitizers and business logic (name sanitizing, duplicate checks, fuzzy
  resolution, color parsing). Kept import-free so it unit-tests in isolation. Raises `GuardError`.
- **`executors.py`** — the actual Discord API calls. `snapshot()` doubles as the pre-request server
  state fed to the model and as the `list_structure` result; it's **lean by default** (ids, names,
  kinds) and only includes the costly permission-overwrite matrix and channel topics when
  `detailed=True`, which `list_structure` requests. Forum and stage channels are listed too (kind
  `forum`/`stage`) so the model's view agrees with `server_stats`. Forums have their own dedicated
  tools (`list_forum_posts`/`create_forum_post`/`reply_to_forum_post`) rather than being folded into
  the generic channel tools — a forum's content is posts (threads), not messages, so `post_message`/
  `edit_channel`/etc. still refuse one by name with a named refusal, not a misleading "no match".
  Stage channels have no tools at all yet — nothing in Roger's surface needs them.

Registry:

| Tool | Mutates? | Confirm? |
|---|---|---|
| `list_structure` | no | — |
| `audit_permissions` | no | — |
| `create_channel` | yes (read_only / private / per-role grants) | only when `private=True` (§2.8) |
| `create_role` | yes | no (always zero-perm, §2.6) |
| `edit_role` | yes (rename/color/hoist/mentionable — never permissions) | **yes** (§2.8) |
| `delete_role` | **yes, irreversibly** (refuses @everyone / managed roles) | **yes** (§2.8, ADR-0007) — diff optionally names current holders (§2.1, ADR-0008) |
| `add_member_role` | yes (refuses @everyone / managed; member by numeric id only) | **yes** (§2.8) — diff shows the role's actual permissions |
| `remove_member_role` | yes (refuses @everyone / managed; member by numeric id only) | **yes** (§2.8) — diff shows the role's actual permissions |
| `set_permissions` | yes | **yes** (§2.8) |
| `edit_channel` | yes (rename/topic/recategorize — never delete) | **yes** (§2.8) |
| `post_message` | side effect (mass mentions suppressed) | **yes** (§2.8) |
| `list_forum_posts` | no | — |
| `create_forum_post` | side effect (mass mentions suppressed) | **yes** (§2.8) |
| `reply_to_forum_post` | side effect (mass mentions suppressed) | **yes** (§2.8) |
| `move_channel` | yes (reorder a channel/category — position only) | **yes** (§2.8) |
| `preview_curated` | no Discord post or seen-state write; spends model budget | no |
| `set_presence` | self only (own status/activity, persisted) | no |
| `set_nickname` | self only (own guild nickname) | no |
| `server_stats` | no | — |
| `add_reaction` | side effect (adds one reaction) | no |
| `remove_reaction` | side effect (removes Roger's own reaction, never another user's) | no |
| `list_role_members` | no | — |
| `list_audit_log` | no | — |
| `list_invites` | no | — |
| `list_webhooks` | no | — |
| `list_scheduled_events` | no | — |

The "toys" are cosmetic and self-directed (presence, nickname) or single-reversible (reactions).
`set_presence` persists its outfit to the `meta` table and is reapplied on every reconnect (Discord
clears presence on reconnect). `add_reaction` / `set_nickname` need the extra gateway scopes noted in
`deploy/README.md`; without them they fail with a clear message instead of breaking anything.

The last five are read-only lookups against Discord's own data, added to close CRUD gaps in the
existing tools rather than copying a general-purpose bot's feature list wholesale (most of that —
leveling, economy, music — is off-shape for a single-guild admin assistant). `list_role_members`
reuses the same on-demand, uncached lookup `delete_role`'s confirm preview already used
(`roger/tools/members.py`, §2.1, ADR-0008) — no new intent. `list_invites` returns metadata but omits
invite codes and URLs so an active access capability never reaches the model. `list_audit_log`,
`list_invites`, and `list_webhooks` need new permissions on Roger's role (View Audit Log, Manage Guild,
Manage Webhooks respectively — see `deploy/README.md`); `list_scheduled_events` needs none, since
scheduled events aren't privileged data. Deliberately **not** built alongside these: `delete_channel`
(channels carry message history — content-bearing, stays under the blanket no-delete rule per
ADR-0007, unlike the zero-permission roles that rule was narrowed for) and anything that mutates or
removes a *member*
(timeout, kick, ban) — those reopen the no-destructive-tools invariant on a person rather than an
inert object and need their own ADR-0007-style discussion before any code, not a silent add.

`add_member_role`/`remove_member_role` touch a member too, but deliberately weren't put in that
same bucket: neither removes anyone from the server or restricts their access — a member keeps
everything they had, the action is trivially reversible, and it's the same risk shape as
`set_permissions` (already shipped, confirm-gated changes to who-can-do-what). The one real
difference from Roger's other role tools: a role Roger *creates* is always zero-permission (§2.6),
but an *existing* role in the server might not be, so assigning one can be a genuine privilege
grant. Handled the same way confirm-gating handles everything else — inform, don't block: the
preview shows the role's actual permission list, not just its name, so the owner approves (or
doesn't) with the real consequence in view. `member` takes a numeric Discord user id only; Roger
can't resolve a member by name without the Members intent (§2.1), the same constraint
`set_permissions`' member targeting already had.

Executors needing more than the guild (store, settings, llm, client) receive a `ToolContext` — a
dependency bag kept `Any`-typed so the tools package never imports the bot/llm/store modules
(no import cycles).

## §8 Ambient brain

Deadpan chat for @mentions and DMs (and `/chat`). **No tools, no authority, ever.** It
keeps a short own-thread memory (per user+channel, from `ambient_log`) and is rate-limited three
ways (§11): per-user, per-user notify-once-then-go-silent, and a global hourly ceiling. Ambient
never touches the admin path.

Roger's emerging character (and where a future personality pass would steer it) is logged in
[`docs/personality.md`](docs/personality.md) — tone only; it never loosens §2/§7/§8.

## §9 Curated news

Scout writes scored feed items and article excerpts to a read-only digest mount. Roger checks the
recent output at `CURATED_HOUR=7` in `TZ` by default, after Scout's 05:30 local run. A positive
`CURATED_CHECK_INTERVAL_MINUTES` enables regular observation instead. The loop starts only when
`CURATED_CHANNEL_ID` is set. Output follows useful developments, with no quota to fill.

- `collect_from_scout` reads a rolling 72-hour window, dedupes overlapping runs, and reports a
  missing or stale producer as a distinct status for ops alerting. Scout owns feed retrieval and
  public page fetching; Roger only reads the output. The 512-file scan cap covers the window at a
  proposed 15-minute collection interval (288 runs) with room for manual runs and restarts.
  Scout currently fetches articles only when it first reports an item, so a same-page refresh does
  not prove current state until [Scout #21](https://github.com/R055LE/scout/issues/21) lands.
- A durable check lease admits one observation at a time, including across processes/restarts.
  The lease lasts 30 minutes and bounds the whole model workflow; an expired worker cannot claim
  a send or overwrite a newer check.
  Each item version fingerprints its bounded model input, item/source identity, model chains, and
  editorial prompts. Unchanged quiet/rejected input is skipped without a model call. Changed input
  or model policy is eligible again. A transient failure gets one retry after an hour; a budget
  deferral gets one on the next local day. Interrupted observations get one after lease expiry.
  A quiet or unusable decision defers every admitted candidate version. An accepted selection
  leaves passed-over items available. Rejection/quiet state never marks a source item seen.
- The curated model sees at most eight candidates with bounded title, summary, match, and article
  text fields. It must return a strict JSON skip or a draft with two to four facts. Each fact needs
  an exact supporting quote from the supplied source excerpt. Invalid responses make no post.
- A separate reviewer checks every fact against its paired quote. A negative verdict allows one
  revision of the same article, followed by strict parsing and a fresh support review. A malformed
  response or a second negative verdict stops before any delivery claim. All calls share the
  Curated budget, and failure statuses include a sanitized reason.
- The public post contains the chosen title, facts, why it matters, optional light take and natural
  question, plus a source link. Discord mentions are suppressed. The owner can call
  `preview_curated` to inspect the decision and supporting quotes without posting or marking an
  item seen; preview still spends from the curated model budget.
- `CURATED_MAX_OBSERVATIONS_PER_DAY` defaults to eight admitted observations, each with at most five
  completion calls (including repairs and reviews). Unchanged input doesn't use this allowance.
  All completions still pass the existing token/dollar gates. The allowance bounds calls even when
  a provider doesn't report usage. Owner previews spend the same model budget but are explicit
  requests and don't consume stream observation state.
- `curated_delivery` reserves the item/event, local-day allowance and spacing, then marks the
  chosen Scout item seen before sending. Capacity counts pending claims as potentially delivered.
  Spacing uses UTC elapsed time across midnight and DST, while daily limits follow `TZ`.
  A successful send records the Discord message ID. If sending has an uncertain outcome, the
  reservation stays pending and Roger will not retry automatically; an operator must reconcile it.
  This prevents duplicate posts after a timeout or crash. The default is still one public send per
  local date; `CURATED_MAX_POSTS_PER_DAY` and `CURATED_MIN_SPACING_MINUTES` configure stream ceilings.
  Event identity currently uses the source URL without its fragment, with a lowercase host; #100
  extends it only when `CURATED_DEVELOPING_STORIES=true`: validated story decisions may follow a
  changed source version through the same delivery ledger. `/status` and sanitized job outcomes report allowances,
  spacing, pending deliveries, last input age, and recent check/editorial outcomes.
- Developing-story mode keeps at most eight Scout sources in an 18,000-character request including
  the serialized response schema, four confirmed prior deliveries, 100 current story pointers, and
  500 immutable decisions for 90 days. Drafts, the one allowed revision, and independent reviews
  use provider-enforced strict JSON schemas plus local parsing, exact-quote validation, and semantic
  review. A valid draft uses two or four model calls; an initial local-validation failure reuses the
  one revision slot and uses three, so every path remains at four or fewer. Actual coverage is derived by
  joining a decision to a `sent` delivery with a message ID; rejected drafts and pending or uncertain
  sends are excluded. Every rendered field carries exact excerpt citations. Current mutable claims
  require a fresh source observation. Preview uses the same checks and model budget while leaving all
  Curated state alone.

Historical Digest and Spark usage rows remain in SQLite after those jobs and tools are retired.

## §10 Persistence

`aiosqlite` in WAL mode, one file under `/data`. Schema creation and idempotent migrations run at
boot. The former per-date Curated ledger migrates atomically, retaining every sent/pending row and
its message reference; seen state remains intact.

| Table | Holds |
|---|---|
| `audit` | Every admin action + gate rejection — the tamper-evident trail |
| `usage` | Daily token spend per brain — drives the budget gate (§11) |
| `seen` | `(feed_url, entry_id)` dedupe keys for curated news (§9) — `feed_url` holds Scout's feed id, not a URL |
| `curated_delivery` | Item/event send claims and delivery state, counted by local date (§9) |
| `curated_check` | Singleton observation lease and daily admission count (§9) |
| `curated_observation` | Item-version decisions and bounded retry state, retained seven days (§9) |
| `curated_story` | Bounded current source pointers and unresolved questions for stable source event IDs (§9) |
| `curated_story_decision` | Immutable story actions, reasons, changes, claims, and exact source snapshots, retained 90 days (§9) |
| `ambient_log` | Ambient own-thread memory, per user+channel (§8) |
| `admin_log` | Owner admin conversation memory, per channel (§6) |
| `gigabrain_log` | Owner gigabrain conversation memory, per channel (§12) |
| `meta` | Small key/value bot state (persisted presence outfit, gigabrain's last-run date) — never pruned |

## §11 LLM layer & budgets

`roger/llm.py` wraps the OpenAI SDK pointed at OpenRouter. Per call: pick the brain's model chain
(§3), **check the daily token cap before spending** (raises `BudgetExceeded` if over), then — if a
`DAILY_USD_<BRAIN>` cap is also set — check accumulated USD spend the same way. The two caps are
layered, not either/or: the token cap always enforces, and the USD cap is an additional, optional
trip wire on top of it. That's deliberate — a provider that never reports cost
(`OPENROUTER_BASE_URL` pointed elsewhere, ADR-0009) would otherwise leave the USD cap permanently
silent, so the token cap stays the real backstop in that case. Once both checks pass, the call
proceeds with automatic fallback down the chain, then **records actual usage** to `usage`. A
missing/empty model chain raises `LLMConfigError`, which callers turn into a plain "not configured"
reply rather than a crash. Real spend is additionally bounded off-box by the OpenRouter key's own
credit limit.

`LLM.preflight()` runs once at boot (folded into the boot self-report, BACKLOG 4.2): a `GET /key`
confirms the configured key is actually accepted, and — if any brain has a model configured — a
`GET /models` confirms every model ID in every brain's chain resolves in OpenRouter's catalog.
Neither call spends a token; both exist so a typo'd key or model ID surfaces at boot instead of on
the first real request, possibly days later. It's boot-only by design, not re-checked on the
watchdog or `/status`: env values can't drift without a restart, so polling them on a timer buys
nothing.

Limits at a glance (defaults; all env-overridable):

| Control | Default |
|---|---|
| Daily tokens — admin / ambient / curated / gigabrain | 150k / 40k / 30k / 100k |
| Daily USD — admin / ambient / curated / gigabrain | off / off / off / off (0 = disabled) |
| Tool calls per admin request | 10 |
| Model round-trips per admin request | 14 |
| Tool calls / round-trips per gigabrain request | 10 / 14 |
| Gigabrain periodic check-in interval | off (0 days) |
| Ambient — per user / window / global hourly | 5 / 600s / 30 |

## §12 Giga Brain — read-only strategic analysis

`roger/brains/gigabrain.py`. Same tool-loop shape as admin (§6) — snapshot, call the model with
tool schemas, validate/guard/execute, feed results back, loop — but with no mutation possible and
no confirm flow anywhere in the module, because neither is ever needed:

- **No mutation, enforced twice.** Its tool schema is built from `GIGABRAIN_TOOLS`, a fixed
  allowlist of the *existing* read-only tools (`list_structure`, `server_stats`,
  `list_role_members`, `list_audit_log`, `list_invites`, `list_webhooks`,
  `list_scheduled_events`, `list_forum_posts`) — the same "expressible actions are bounded, not
  prompted" philosophy as §2.6/§2.7, applied to a whole brain instead of one field. `_run_tool`
  additionally refuses at execution time if a model ever calls a tool name outside that allowlist,
  or one that (contrary to how the allowlist was chosen) turns out to need confirmation — belt and
  suspenders, not just "the schema doesn't offer it."
- **Owner-only, single-guild** (§2.2/§2.3) — the `/gigabrain` command gate mirrors `/roger`'s
  exactly (`CANNED_DENY`, `AuditStatus.GATE_REJECTED`, zero tokens spent on a non-owner).
- **Its own model chain and budget** (`MODEL_GIGABRAIN`, `DAILY_TOKENS_GIGABRAIN`), tuned for depth
  rather than speed: lower temperature and a higher `max_tokens` ceiling than admin (§11), plus an
  opt-in `GIGABRAIN_REASONING_EFFORT` passthrough to OpenRouter's unified `reasoning.effort` param
  — sent only if set, so it's a no-op unless the configured model supports it.
- **Its own conversation memory** (`gigabrain_log`, §10) so a follow-up in the same channel has
  context, kept separate from admin's memory the same way ambient's is.
- System prompt instructs the model to phrase output as analysis/suggestions, never as actions
  taken, and to point the owner at `/roger` (admin) for anything concrete enough to execute.

Triggered two ways: on demand via `/gigabrain <question>`, and optionally on a periodic,
unprompted check-in (`run_gigabrain_suggestion`) — a fixed "review the server, propose
improvements" prompt through the same read-only loop. Delivered to `GIGABRAIN_CHANNEL_ID` if set,
else **DMed directly to the owner** — DM is the safer default (these can be candid, owner-only
musings, unlike the curated public post), but a dedicated private channel is supported for anyone
who'd rather have a scrollable history than a string of DMs; either way it's the deploy owner's
choice of destination and privacy, not Roger's. Off by default (`GIGABRAIN_INTERVAL_DAYS=0`); when
set, a daily `tasks.loop` tick at `GIGABRAIN_HOUR` calls it unconditionally and the function decides
for itself whether it's actually due, the same "the job decides, not the caller" shape as
the curated job's quiet-day status. Cadence is tracked via a `gigabrain_last_run_date` key in `meta`
(§10) rather than an in-memory guard, because a several-day interval must survive a mid-cycle
restart. A failed delivery (DM closed, bot blocked, configured channel not found or not
postable) is logged and surfaced once via the ops watchdog, not retried — the next scheduled tick
will naturally try again once the interval elapses.

The check-in uses the delivery destination's id as `channel_id`, not `None` — it
*should* have continuity: each run sees prior check-ins via the same `recent_gigabrain` memory an
interactive conversation uses. The prompt is explicit about using that memory well: lead with
what's changed, stay brief when nothing has, and don't re-flag the same unaddressed suggestion
every single cycle — repetition-by-default is exactly the "why aren't you doing anything" nagging
a periodic advisor should avoid.
