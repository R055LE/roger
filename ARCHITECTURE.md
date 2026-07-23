# Architecture

How Roger is put together and why. Source comments cite these sections as `(§N)` — this file is
what they point to. It's a design reference, not a spec to implement against; the code is the
source of truth, and where they ever disagree, the code wins.

## §1 Overview

Roger is a **single-guild, owner-gated Discord assistant** built on hosted models via OpenRouter.
It runs as one process with three independent **brains**, chosen entirely by *who* is talking and
*where*:

| Brain | Purpose | Tools | Who |
|---|---|---|---|
| **Admin** (§6) | Server concierge — creates channels/roles, sets permissions, curates feeds | Yes | Owner only |
| **Ambient** (§8) | Deadpan chat persona | None | Anyone |
| **Digest** (§9) | Scheduled RSS/Atom summary | n/a (scheduled) | — |

No agent framework. The admin brain is a hand-rolled tool loop (§6) so every step is inspectable
and bounded. The design goal is that **safety is structural** — enforced by which intents are off,
which tools exist, and which permissions are expressible — not by prompt wording (§2).

## §2 Security invariants

These hold regardless of what any model outputs. They are the load-bearing part of the design.

- **§2.1 No privileged gateway intents.** The client uses `Intents.default()` — `message_content`,
  `members`, and `presences` stay **off**, asserted at startup (`_assert_non_privileged`). Roger
  only ever sees content in DMs, @mentions, and its own messages.
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
- **§2.5 No destructive or escalating tools.** Nothing Roger can do is irreversible: there is no
  delete, kick, ban, or bulk-purge tool anywhere in the surface. Roger *creates*, and it *adjusts*
  existing state — renaming a channel, editing a topic, moving it under a category, setting channel
  overwrites, posting a message — but every adjustment is reversible and confirm-gated (§2.8). The
  blast radius is bounded by what simply doesn't exist: no tool destroys anything.
- **§2.6 Roles are created with zero permissions.** `create_role` always passes
  `Permissions.none()`; access is granted through channel overwrites, never role permissions.
- **§2.7 Permission allowlist.** Only a fixed set of overwrite bits is expressible through the
  tool schema (§7). Anything outside the allowlist is *unrepresentable* — the model literally
  cannot ask for it.
- **§2.8 Confirm-gated mutations.** Every tool that changes *existing* state — `set_permissions`,
  `edit_channel`, `post_message` — requires interactive owner approval against a rendered diff before
  it runs. Creation is exempt by default: `create_channel` / `create_role` add new, empty,
  zero-permission objects, and setting a brand-new channel's access at creation (`read_only`,
  per-role `grants`) has nil blast radius — no members, no history — so it applies immediately.
  Whenever a creation overwrite restricts @everyone, Roger also grants *itself* an overwrite so it is
  never locked out of a channel it just made. The **one deliberate exception is
  `create_channel(private=True)`**: hiding a channel is still nil-blast-radius, but it is
  confirm-gated anyway — a hidden channel is a surprising side effect, and keeping the confirm ritual
  consistent for security-relevant actions is worth more than shaving a click. Confirmation can thus
  be *static* (a tool always confirms) or *conditional on the args* (`create_channel` confirms only
  when `private`), via `ToolSpec.needs_confirm`.
- **§2.9 Budgets.** A hard cap of **5 tool calls per request** (`MAX_TOOL_CALLS`) and **8 model
  round-trips** (`MAX_TURNS`), plus per-brain **daily token caps** (§11) checked before every call.
- **§2.10 No secrets in git — ever, not even encrypted.** Secrets live only in a `sops`+`age`
  encrypted `roger.env` on the host. The repo carries `.sops.yaml` (the public recipient) and
  `roger.env.example`. See [`deploy/`](deploy/README.md).

## §3 Configuration

All settings load from the process environment via `pydantic-settings` (`roger/config.py`), injected
at runtime by `sops exec-env`. Nothing is read from a committed file. Notable shapes:

- `MODEL_ADMIN` / `MODEL_AMBIENT` / `MODEL_DIGEST` are **comma-separated priority chains** — primary
  first, the rest are OpenRouter fallbacks. Every model in the admin chain must support tool calling.
- `OPENROUTER_BASE_URL` is config, so pointing Roger at a local inference host is an env change.
- `DIGEST_FEEDS` seeds the feed list **once** (§9); after that the store owns it.

## §4 Runtime & process model

One `asyncio` process (`python -m roger`). Non-root, read-only root filesystem, `/tmp` on tmpfs,
one writable bind mount at `/data` for the SQLite DB. Structured JSON logs to stdout
(`_JsonFormatter`); discord.py's gateway chatter is pinned to WARNING. `discord.py`'s
`ext.tasks` drives the daily digest loop (§9).

## §5 Dispatch & routing

`classify_message` is a **pure** function (no side effects, unit-tested with fakes) that maps an
incoming message to a `Route`. Because `message_content` is off (§2.1), guild messages that neither
mention Roger nor arrive in a DM show up with empty content and are ignored by design.

| Condition | Route | Brain |
|---|---|---|
| Author is Roger | `IGNORE` | — |
| Empty content | `IGNORE` | — |
| DM from owner | `ADMIN_DM` | Admin |
| DM from non-owner | `AMBIENT_DM` | Ambient |
| Guild @mention from owner | `ADMIN_MENTION` | Admin |
| Guild @mention from non-owner | `AMBIENT_MENTION` | Ambient |
| Guild, no mention | `IGNORE` | — |

Slash commands bypass classification: `/roger <request>` → admin (owner-gated), `/chat <message>` →
ambient (open to anyone). On the mention routes the leading mention is stripped first so the model
sees a clean request; an empty remainder is dropped.

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
Bounds: 5 tool calls, 8 turns (§2.9).

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
  `detailed=True`, which `list_structure` requests.

Registry:

| Tool | Mutates? | Confirm? |
|---|---|---|
| `list_structure` | no | — |
| `create_channel` | yes (read_only / private / per-role grants) | only when `private=True` (§2.8) |
| `create_role` | yes | no (always zero-perm, §2.6) |
| `set_permissions` | yes | **yes** (§2.8) |
| `edit_channel` | yes (rename/topic/move — never delete) | **yes** (§2.8) |
| `post_message` | side effect (mass mentions suppressed) | **yes** (§2.8) |
| `run_digest` | side effect | no |
| `list_feeds` | no | — |
| `suggest_feeds` | no (validates only) | — |
| `add_feed` | yes | no |
| `remove_feed` | yes | no |

Executors needing more than the guild (store, settings, llm, client) receive a `ToolContext` — a
dependency bag kept `Any`-typed so the tools package never imports the bot/llm/store modules
(no import cycles).

## §8 Ambient brain

Deadpan chat for @mentions and non-owner DMs (and `/chat`). **No tools, no authority, ever.** It
keeps a short own-thread memory (per user+channel, from `ambient_log`) and is rate-limited three
ways (§11): per-user, per-user notify-once-then-go-silent, and a global hourly ceiling. Ambient
never touches the admin path.

## §9 Digest brain

A scheduled RSS/Atom summary, on a daily `tasks.loop` fired at `DIGEST_HOUR` in `TZ`, also
triggerable via the `run_digest` tool. There is **no user input anywhere in this path**.

- **Feed list is store-owned.** `DIGEST_FEEDS` seeds the `feeds` table **once** on first run
  (`seed_feeds_if_empty`); after that Roger curates it live via `suggest_feeds` / `add_feed` /
  `remove_feed`, and the env var only acts as the default set that returns if the list is ever fully
  cleared. `suggest_feeds` and `add_feed` fetch each candidate and confirm it parses as a live feed
  before recommending/storing it — the model proposes, the tool grounds it in reality.
- **Robust collection.** One dead feed never kills a run. Entries cap at `MAX_ITEMS` (15), summaries
  are truncated to 500 chars before the model sees them.
- **Exactly-once posting.** Items are marked **seen** (`seen` table) only *after* a successful post,
  so a failed post retries the same items next time rather than dropping them.

## §10 Persistence

`aiosqlite` in WAL mode, one file under `/data`. The full schema is created up front so new
behaviour adds rows, not migrations.

| Table | Holds |
|---|---|
| `audit` | Every admin action + gate rejection — the tamper-evident trail |
| `usage` | Daily token spend per brain — drives the budget gate (§11) |
| `seen` | `(feed_url, entry_id)` dedupe keys for the digest (§9) |
| `ambient_log` | Ambient own-thread memory, per user+channel (§8) |
| `admin_log` | Owner admin conversation memory, per channel (§6) |
| `feeds` | The curated digest feed list (§9) |

## §11 LLM layer & budgets

`roger/llm.py` wraps the OpenAI SDK pointed at OpenRouter. Per call: pick the brain's model chain
(§3), **check the daily token cap before spending** (raises `BudgetExceeded` if over), call with
automatic fallback down the chain, then **record actual usage** to `usage`. A missing/empty model
chain raises `LLMConfigError`, which callers turn into a plain "not configured" reply rather than a
crash. Real spend is additionally bounded off-box by the OpenRouter key's own credit limit.

Limits at a glance (defaults; all env-overridable):

| Control | Default |
|---|---|
| Daily tokens — admin / ambient / digest | 150k / 40k / 30k |
| Tool calls per admin request | 5 |
| Model round-trips per admin request | 8 |
| Ambient — per user / window / global hourly | 5 / 600s / 30 |
