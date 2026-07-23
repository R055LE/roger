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

### 1.2 Make the ops channel an alerting surface, not just a boot ping — **S/M**
`_post_ops` already exists and works; the only thing that ever calls it is `_startup_report`. Wire it
to the events an operator actually wants pushed:

- **Budget threshold** — first crossing of 80% of any brain's daily cap (once/day/brain).
- **Digest failure** — `run_digest_job` returning a non-ok status.
- **Permission loss** — a required scope missing on a later check, not just at boot.
- **LLM error spike** — repeated `APIConnectionError`/`BudgetExceeded` inside a short window.

Keep each alert idempotent (dedupe key + cooldown) so a flapping condition can't spam the channel.

*Why:* the alerting surface is already built and sitting idle. This is the cheapest large jump in
operational awareness on the list, and it's on-brand with the SRE observability lab's alerting work.

### 1.3 Data retention & pruning — **S**
Every table in `store.py` grows unbounded: `audit`, `ambient_log`, `admin_log`, `seen`. Two problems:
disk creep over years, and `ambient_log` retaining *other users'* message content indefinitely (a
privacy posture issue, not just hygiene).

- A startup + daily prune: delete `ambient_log` / `admin_log` older than N days (env-tunable, e.g.
  30), `seen` older than the longest feed's practical re-post window, and cap `audit` by age or row
  count (audit is the tamper-evident trail — prefer a generous age window over aggressive trimming).
- `PRAGMA wal_checkpoint` / periodic `VACUUM` so the file actually shrinks.

*Why:* unbounded PII retention is the kind of thing a security reviewer flags on sight, and it's a
one-file change.

### 1.4 Liveness: `HEALTHCHECK` + heartbeat — **S**
The `Dockerfile` has no `HEALTHCHECK`, so a wedged event loop or a gateway that reconnects-forever is
invisible to Docker and the systemd deploy unit — the container stays "up" while doing nothing.

- Heartbeat: touch a file under `/tmp` (already tmpfs) from the digest loop / an `on_socket_response`
  tick; `HEALTHCHECK` asserts its freshness. Or expose the metrics endpoint from **3.1** and health-
  check that — one server, two wins (preferred if 3.1 lands).

*Why:* "the container is running" and "the bot is working" are different claims; right now only the
first is observable.

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

### 1.5 LLM request timeout + smarter retry — **S**
`_call_with_one_retry` retries **once**, and only on `APIConnectionError` / `APITimeoutError`. Two
gaps: (a) no explicit per-request timeout, so a hung call can sit on an already-`defer()`ed
interaction up to the SDK default; (b) `429` / `5xx` aren't retried at all (OpenRouter's `models`
array handles *provider* failover, but not an account-level rate limit or a transient gateway 5xx).

- Set an explicit timeout on the client (or per call, tighter for ambient than admin).
- Add bounded exponential backoff on `429` / `5xx`, honoring `Retry-After` when present.

*Why:* the current retry is thinner than the module docstring implies, and a deferred admin
interaction is exactly where a hang is most visible to the owner.

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

### 2.3 Coverage measurement + threshold — **S**
CI runs `pytest` with no coverage gate. Add `pytest-cov` with a floor (the global rules target 80%)
so a green check means "covered," not just "didn't crash." Publish the number in the run summary.

### 2.4 Pin the base image by digest — **S**
`Dockerfile` uses `python:3.12-slim` — a moving tag, which contradicts the "pin versions, no `latest`"
principle. Pin to a `@sha256:` digest and let Dependabot bump it (Dependabot already runs; extend it
to Docker if it isn't watching the base).

---

## Tier 3 — Observability bridge (AI + SRE visibility)

Directly serves the "public AI/ML work + infra bridges" goal.

### 3.1 Prometheus `/metrics` endpoint — **M**
Structured JSON logs exist, but there's no metrics surface — a notable omission next to a dedicated
SRE observability lab. Stand up a tiny aiohttp server (bound to the container only) exposing:

- token **and dollar** spend per brain (from **1.1**), as counters,
- tool calls by tool × outcome (`ok`/`denied`/`invalid`/`error`),
- digest runs (success/failure) and items posted,
- ambient rate-limit rejections (per-user / global),
- LLM errors by type and `BudgetExceeded` events.

Doubles as the health target for **1.4**. Ship an example Grafana panel JSON to make the bridge to the
observability lab legible to a reader.

*Why:* turns Roger into a live exhibit for the SRE lab's dashboards and the clearest single "LLM app +
production observability" artifact in the portfolio.

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
