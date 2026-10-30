# Story model evaluation

`python -m roger.story_eval` prints a network-free plan for the Curated story
comparison. It reads the synthetic cases and fixed model manifest, then lists
every request and its conservative reservation. It does not read credentials,
Scout, the production database, or Discord.

The prepared screen is 48 writer calls (four models, six cases, two repeats)
and 48 reviewer calls (two models, twelve calibration fixtures, two repeats).
Each call reserves 32,768 input tokens and either 16,384 writer tokens or 8,192
reviewer tokens. Reasoning tokens count against the output ceiling. Input uses
the greater of the base price and the total price including possible cache
writes, even when the request asks the provider not to cache. The current
per-call ceilings total $30.00 for the prepared 96-call screen. These are
reservation bounds, not expected spend or an approved allowance.

The original proposal reserved $92.60 within a $100 envelope for 196 calls.
That proposal predates cache-write-aware pricing. It remains historical evidence;
new plans and approvals must use the current calculation without rewriting old
ledgers or transferring unused allowances.

The initial screen uses separate immutable ledgers, so the operator must track
the aggregate approval across them:

| Run | Calls | Per-run ceiling |
| --- | ---: | ---: |
| Astra writer | 12 | $14.76 |
| Sol writer | 12 | $3.00 |
| Sonnet writer | 12 | $3.00 |
| Gemini writer | 12 | $1.08 |
| Sol reviewer | 24 | $4.08 |
| Sonnet reviewer | 24 | $4.08 |

The worst-case selected pairing is 48 calls and $33.60 when Astra writes and
Sol or Sonnet reviews. Two such pairing ledgers reserve 96 calls and $67.20.
No single ledger enforces the aggregate allowance across other artifact
directories.

Every paid comparison requires explicit owner approval for its finite call and
cost allowance, plus a new immutable ledger in its own artifact directory.
Recheck the model catalog, endpoint, provider and rates in
`evals/stories/models.json` before proposing a run. In particular, the Google
endpoint's catalog status is recorded without interpreting it. Do not
substitute another provider or service tier under the same approval.

## Inspect the plan

```bash
python -m roger.story_eval plan
```

The plan reports the exact dated model, accepted returned IDs, provider,
service tier, settings, input reservation, output ceiling and cost reservation
for each call. It does not construct an API client. The request omits
temperature, `top_p`, model chains and fallback. It requires strict JSON,
medium reasoning, one first-party provider and the standard price ceiling.

## Run an approved slice

Use a new artifact directory outside the repository. A directory is created
exclusively and is never resumed or reused. `OPENROUTER_API_KEY` is read only at
runtime and is not written to artifacts.

```bash
OPENROUTER_API_KEY=... python -m roger.story_eval run \
  --stage writer --model sol --case straight_release --repeats 2 \
  --artifact-dir /tmp/roger-story-eval-sol \
  --max-calls 2 --max-cost-usd 0.50 --approve-paid-calls
```

Reviewer calibration uses `--stage reviewer --model sol`; each `--case` value
then names a reviewer fixture. Complete workflow testing uses `--stage
workflow --writer-model sol --reviewer-model sonnet`. The workflow calls the
production `stories.decide` path and remains bounded to four calls.

A reviewed held-out current-evidence packet may replace the fixture file with
`--fixtures /path/to/reviewed.json`. Each case supplies its own aware `now`, raw
entries, confirmed history, author reference expectation and human rubric. The
harness still reads only that file. It never fetches live evidence. Use `plan
--fixtures ...` before asking for paid-run approval.

Before every network request, the ledger durably reserves one call and its full
cost ceiling. Calls are sequential. There are no SDK retries, model fallbacks,
or automatic repeats. A request failure or uncertain result retains its
reservation. Missing or invalid usage totals, cost, route metadata, exact model
identity, or selected provider fails closed. A missing
optional reasoning-token breakdown stays `null`; total output still bounds and
bills it. A missing returned service tier is recorded as unknown and limits
latency comparisons, while an explicitly different tier fails closed. Provider
truncation, refusal, content filtering, pipeline interference, local parsing,
reviewer rejection and human editorial quality remain separate outcomes.
The exact pass-only OpenAI moderation annotation with `flagged: false` is
recorded as structured routing metadata and may proceed. Any extra action,
redaction or unknown field, a flagged result, an unknown stage, or a modifying
stage such as response healing, context compression, a plugin or server tools
stops the run.
Reviewer calibration has six supported references and six single-fault drafts.
Any false accept, false reject, malformed review, incomplete run, or missing
fixture repeat fails its reference gate. Reference labels are still pending
human confirmation, so this gate alone cannot select a reviewer.
Reviewer requests are built and verdicts are parsed by the production helpers. They include the
actual public embed fields plus bounded selected-story evidence for material-omission review;
broader evidence cannot substitute for a proposed item's own cited spans.
The stage item's own quote must support the rendered development's stage. In particular,
`demonstrated` needs an observed result rather than test setup alone, while `uncertainty` needs an
explicit unresolved limit.
The repository's regressions are derived synthetic reproductions. Exact captured outputs stay in
their immutable evaluation bundle.

Response caching is disabled with `X-OpenRouter-Cache: false`. Provider prompt
caching is separate. OpenAI profiles request it off using
`prompt_cache_options: {"mode": "explicit"}` without breakpoints. Other profiles
record `provider_default`. Messages must be plain text, without explicit cache
markers or TTLs that would invalidate the price bound. Sonnet's input bound
covers default five-minute writes. Gemini implicit caching adds no write/storage
charge, so its total input price remains the base price; the catalog's explicit
storage surcharge is not a total input-token price. Missing or invalid cache
policy/pricing rejects the profile before a request can be sent.

These settings follow the [provider caching documentation](https://openrouter.ai/docs/guides/best-practices/prompt-caching).
SDK tests verify serialization, not live endpoint acceptance of the new control.
Positive reported prompt-cache activity under requested-off stops the run after
recording the reported cost. Missing counters remain unknown; two zero counters
mean `none_reported`, not independently verified absence of caching.

Artifacts retain allowlisted IDs in `response.generation_id`, and cache-read/write
token counts and upstream cost components in `response.usage_details`, even when
required usage is missing or the response is malformed. Unknown or invalid optional fields
stay `null` and do not replace a valid provider-reported charge. A cost difference
alone does not establish cache activity. Earlier artifacts are not rewritten.
Artifacts also contain the complete visible response text, an allowlisted routing-metadata
view, token/cost accounting, sanitized error classes or status codes, and the
exact messages, schema and non-secret request settings needed to reproduce the
call. They do not contain the key, authorization headers, hidden reasoning or
raw exception text.

## Assess results

```bash
python -m roger.story_eval summarize /tmp/roger-story-eval-sol
```

`summary.json` leaves `human_rating` and `human_acceptance` pending. The fixture
labels and reference decisions are author expectations pending human review.
A parser pass or reviewer agreement is not human acceptance. Human assessment
scores useful detail, focus, what changed, grounded commentary, honest
uncertainty and readable public copy. A hold or skip on a reportable case is a
failure.

Each writer and workflow result records the original and delivered excerpt
lengths and any omitted characters. The prepared synthetic cases fit inside the
current 1,600-character per-source cap. If held-out evidence is omitted, report
that limitation before interpreting quality. A fuller-evidence diagnostic would
need an explicit non-production profile using the same original source text;
it cannot count as production acceptance or silently relax the parser.
