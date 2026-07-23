# Backlog

Roger is feature-complete for its stated scope (see [`README.md`](README.md) → Status): three brains,
the bounded admin tool loop, feed curation, `/status`, and the boot self-report all ship and are
tested. This backlog is therefore **not** a feature wishlist — it's the production-hardening layer
that separates "runs on my homelab" from "portfolio-grade LLM service," ordered by value-for-effort.

Each item names the concrete gap in the current code, a proposal, a rough effort, and why it earns
its place. Items are grouped in tiers; within a tier, higher is higher-priority. Nothing here
loosens a security invariant in [`ARCHITECTURE.md`](ARCHITECTURE.md) §2 — several tighten it.

Effort key: **S** ≈ an afternoon, **M** ≈ a day or two, **L** ≈ multi-day.

---

## Tier 1 — Production readiness

Gaps that matter for a bot that's actually live. These are the ones I'd do first.

### 1.1 Track spend in dollars, not just tokens — **M** — *visibility shipped; gate remains*
`llm.py` records `prompt_tokens` / `completion_tokens` per brain (`add_usage`) and the daily cap is a
raw token count. But a brain's model chain mixes models at very different prices, so a token budget
is a weak proxy for the thing that actually costs money. OpenRouter returns the real generation cost
(a `cost` field on the response `usage` object, always included now).

- [x] Add a `cost_usd` column to the `usage` table (with an idempotent migration for live DBs);
      capture the OpenRouter-reported cost per call in `LLM.complete`. *(a2689b5)*
- [x] Surface per-brain and total `$ today` in `/status`. *(a2689b5)*
- [ ] Make the daily gate dollar-denominated (env: `DAILY_USD_*`) with the token cap as the fallback
      when a provider doesn't report cost. Deferred: enforcement is a semantic change, kept out of the
      visibility commit.

*Why:* the single most portfolio-differentiating item here — real LLM cost governance is exactly the
infra+AI bridge the portfolio is aiming at, and it's the honest version of the budget the code
already pretends to enforce.

### 1.2 Make the ops channel an alerting surface, not just a boot ping — **S/M** — *mostly shipped*
`_post_ops` already exists and works; the only thing that used to call it was `_startup_report`. Now
wired to the events an operator actually wants pushed, via an in-memory `OpsNotifier` (per-key
cooldown) and a `_watchdog` `tasks.loop` (every 10 min, started only when an ops channel is set):

- [x] **Budget threshold** — first crossing of 80% of any brain's daily token cap, quoting tokens +
      $, deduped once per brain per day. *(next commit)*
- [x] **Permission loss** — a required scope missing on a later sweep (not just at boot), re-reminding
      every 6h while broken. *(next commit)*
- [x] **Digest failure** — `run_digest_job` returning anything but a known-OK status, hooked on the
      digest loop and deduped per day. *(next commit)*
- [ ] **LLM error spike** — repeated `APIConnectionError` / `BudgetExceeded` inside a short window.
      Deferred: needs error-rate tracking, more infra than the other three combined.

Each alert is idempotent (dedupe key + cooldown) so a flapping condition can't spam the channel.

*Why:* the alerting surface was already built and sitting idle. Cheapest large jump in operational
awareness on the list, on-brand with the SRE observability lab's alerting work.

### 1.3 Data retention & pruning — **S** — *shipped*
Every table in `store.py` grew unbounded: `audit`, `ambient_log`, `admin_log`, `seen`. Two problems:
disk creep over years, and `ambient_log` retaining *other users'* message content indefinitely (a
privacy posture issue, not just hygiene).

- [x] `Store.prune()` deletes past-window rows — `ambient_log`/`admin_log` at 30d, `seen` at 90d,
      `audit` at 365d (the tamper-evident trail, kept longest) — via a `RETENTION_DAYS` table.
- [x] Runs at boot and once per calendar day thereafter (`_maybe_prune`, date-guarded, also driven by
      the watchdog). Idempotent.
- [x] `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM` (with cursors closed first) so the file shrinks.

*Why:* unbounded PII retention is the kind of thing a security reviewer flags on sight; one-file
change. Retention windows are module constants for now — promote to env/runtime config if needed.

### 1.4 Liveness: `HEALTHCHECK` + heartbeat — **S** — *shipped*
The `Dockerfile` had no `HEALTHCHECK`, so a wedged event loop or a gateway that reconnects-forever was
invisible to Docker — the container stayed "up" while doing nothing.

- [x] `_heartbeat` `tasks.loop` (60s) rewrites `/tmp/roger.healthy` (already tmpfs). A turning event
      loop keeps it fresh; a wedge lets it go stale.
- [x] `roger/health.py` (`python -m roger.health`) checks the file's mtime against `MAX_AGE_S` (180s
      = 3 missed beats); the `Dockerfile HEALTHCHECK` runs it. Import-free, so it's cheap and unit-
      tested.

*Why:* "the container is running" and "the bot is working" are different claims; now the second is
observable in `docker ps`. (Auto-restart on unhealthy is a separate host concern — Docker doesn't
restart on health alone; an `autoheal` sidecar or systemd check would close that, out of scope here.)

### 1.6 Enrich the boot self-report — **S** — *shipped*
The boot report was a bare line ("✅ roger online — <guild>. All required permissions present.") —
correct, but almost no signal on a deploy.

- [x] Reuse the `gather_status` readout as the report body, so every deploy pushes a full snapshot
      (permissions · token/dollar spend · digest schedule · feeds · recent actions) instead of one
      line. *(364ce44)*
- [x] Add deployed **build identity** to the header — a `ROGER_VERSION` baked into the image at build
      time (Dockerfile `ARG` ← `github.sha`), read as image metadata (not Settings/compose), so the
      report answers "which build just came up?" — the one thing `/status` can't. *(364ce44)*

*Why:* the ops channel is a deploy-notification surface; on a pull-based CD pipeline "what version is
now live, and is it healthy?" is exactly the question a deploy ping should answer. Composes with
**1.2** (event alerts) and reuses **1.1** (cost).

### 1.5 LLM request timeout + smarter retry — **S** — *shipped*
`_call_with_one_retry` retried **once**, and only on `APIConnectionError` / `APITimeoutError`. Two
gaps: (a) no explicit per-request timeout, so a hung call could sit on an already-`defer()`ed
interaction up to the SDK default; (b) `429` / `5xx` weren't retried at all.

- [x] Explicit `timeout=REQUEST_TIMEOUT_S` (60s) on the client — a hung call can't camp on a deferred
      interaction.
- [x] `_call_with_retries`: bounded exponential backoff (`MAX_ATTEMPTS=3`) over transport errors,
      timeouts, `429` (`RateLimitError`), and all `5xx` (`InternalServerError`), honoring a numeric
      `Retry-After`. Everything else (4xx, config) still fails fast.

*Why:* the old retry was thinner than the module docstring implied, and a deferred admin interaction
is exactly where a hang is most visible to the owner.

---

## Tier 2 — Supply chain & CI hardening

On-brand with the container-hardening and IaC-security labs — and currently the repo stops just short
of its own preached bar.

### 2.1 Sign the image with Cosign (keyless) + verify at deploy — **M**
`release.yml` attests SBOM and provenance but doesn't sign the image. Add keyless (OIDC) `cosign sign`
in the release workflow, and a `cosign verify` gate in `roger-deploy.sh` before `docker compose up`.

*Why:* closes the supply-chain loop the container-hardening-lab already documents, and makes the pull-
based deploy verify *what* it's pulling — not just that a digest changed. Sets up a future Kyverno/
admission verify story if Roger ever lands on the k8s-bootstrap-lab.

### 2.2 Dependency + image vuln scanning in CI — **S**
No `pip-audit` on the locked deps and no image scan (Trivy/Grype) in `release.yml`, despite the global
"check for CVEs before pinning" principle. Add `pip-audit` to the `ci` job and a Trivy scan of the
built image to `release` (fail on fixable HIGH/CRITICAL). `ruff`'s `S` (bandit) rules already cover
SAST, so this is the missing supply-chain half.

*Why:* dependency CVEs are the most common way a "finished" project rots; automating the check is the
whole point of the labs this repo sits beside.

### 2.3 Coverage measurement + threshold — **S** — *shipped*
CI ran `pytest` with no coverage gate. Now `pytest-cov` runs in CI with `--cov-fail-under=75` and a
term-missing report.

- [x] `pytest-cov==7.1.0` dev dep; `[tool.coverage.run]` omits the `__main__` entrypoint.
- [x] CI gate at 75% (current total 78.6%; the testable core is 90–100%, `bot.py`'s Discord glue is
      the drag — a regression guard, not a purity bar).

### 2.4 Pin the base image by digest — **S** — *shipped*
`Dockerfile` used `python:3.12-slim` — a moving tag, contradicting "pin versions, no `latest`."

- [x] Pinned to `python:3.12-slim@sha256:57cd7c3a…` (tag kept for readability).
- [x] Extended Dependabot with a `docker` ecosystem so the digest + comment get bumped weekly.

---

## Tier 3 — Observability bridge (AI + SRE visibility)

Directly serves the "public AI/ML work + infra bridges" goal.

### 3.1 Prometheus `/metrics` endpoint — **M** — *shipped*
Structured JSON logs existed, but there was no metrics surface — a notable omission next to a
dedicated SRE observability lab. Now `prometheus_client` serves `/metrics` on `METRICS_PORT` (9108)
from a background thread; an async 30s loop refreshes the SQLite-sourced gauges (`roger/metrics.py`).

- [x] Token **and dollar** spend per brain, plus caps, as gauges (from **1.1**).
- [x] Tool calls by tool × outcome — sourced free from the `audit` table (`roger_audit_events`).
- [x] LLM requests, errors by type, and budget rejections — in-process counters in `llm.py`.
- [x] Feed count and `roger_build_info{version}`.
- [x] Example Prometheus scrape job + importable Grafana dashboard in `deploy/observability/`.
- [ ] Not yet wired: digest run and ambient rate-limit counters (both are cheap follow-ups at their
      call sites). The audit table already covers the admin/tool surface.

*Why:* turns Roger into a live exhibit for the SRE lab's dashboards — the clearest single "LLM app +
production observability" artifact in the portfolio. Note: publishing the port on the host is a
deliberate, confirm-first step (see deploy notes); the repo change alone doesn't expose it.

### 3.2 Correlation IDs through logs + audit — **S**
Thread a short request ID from each admin/ambient entry point through the log records and the `audit`
rows for that request, so a single interaction is greppable end-to-end across the JSON logs.

---

## Tier 4 — Runtime config & smaller niceties

### 4.1 Runtime settings table — **M**
A `settings` table so operational values (`ops_channel_id`, `digest_hour`, `digest_channel_id`, per-
brain caps) can change at runtime — via an owner-only `/config` tool — without a `sops` edit + redeploy
cycle. Env stays the seed/default; the store owns the live value once set (the same "seed once, store
owns it" pattern the feed list already uses). Keeps secrets in env; only non-secret operational knobs
move to the store.

### 4.2 Config preflight at boot — **S**
Nothing validates the OpenRouter key or the configured model IDs until the first real call fails. A
lightweight boot preflight (validate the key, resolve the model chains against OpenRouter's model
list) reported through the boot self-report makes a misconfiguration obvious immediately instead of on
first use.

### 4.3 Periodic feed-health check — **S**
`suggest_feeds` / `add_feed` validate a feed *at add time*, but a feed can go dead later and silently
contribute nothing. A weekly health pass that reports newly-dead feeds to the ops channel keeps the
curated list honest.

### 4.4 `/data` backup runbook — **S**
The SQLite DB is the only stateful thing and has no documented backup. Add a host-side runbook entry
(periodic `sqlite3 .backup` or Litestream to object storage) to `deploy/README.md`. Host-side/docs
only — no app change.

---

## Deliberately not doing (for now)

Restraint is a design decision; these are logged as considered-and-declined so they don't get
re-proposed:

- **Cross-source digest dedup** (same story from two feeds). Real effort, marginal value for a
  personal digest; per-`(feed, entry)` dedup is enough.
- **"Undo last action."** Every mutation is already confirm-gated and reversible-in-principle; a
  generic undo is a lot of state machinery for a bot that intentionally has no destructive tools.
- **Thread creation/management tools.** Scope creep against the deliberately small, bounded tool
  surface (§2.5); the security story is stronger for it staying small.
- **Ambient allow/blocklists.** Over-engineering for a single-guild personal bot; the global hourly
  cap already bounds cost/abuse.
- **Multi-guild support.** Directly contradicts the single-guild invariant (§2.2) the whole security
  model rests on. A non-goal, not a backlog item.
