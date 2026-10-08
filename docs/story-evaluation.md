# Story model evaluation

`python -m roger.story_eval` prints a network-free plan for the Curated story
comparison. It reads the synthetic cases and fixed model manifest, then lists
every request and its conservative reservation. It does not read credentials,
Scout, the production database, or Discord.

The prepared screen is 48 writer calls (four models, six cases, two repeats)
and 48 reviewer calls (two models, twelve calibration fixtures, two repeats).
Each call reserves 32,768 input tokens and either 16,384 writer tokens or 8,192
reviewer tokens. Reasoning tokens count against the output ceiling. The current
per-call ceilings total $27.60, so the proposed initial allowance is 96 calls
and $28. Up to two selected writer/reviewer pairings can later use 96 calls and
$64. A separately reviewed current-evidence acceptance packet can use four more
calls and $2.60. The exact reserved total is $92.60, with a proposed $100 outer
envelope for 196 calls. These are approval bounds, not expected spend.

The initial screen uses separate immutable ledgers, so the operator must track
the aggregate approval across them:

| Run | Calls | Per-run ceiling |
| --- | ---: | ---: |
| Astra writer | 12 | $13.80 |
| Sol writer | 12 | $2.76 |
| Sonnet writer | 12 | $2.76 |
| Gemini writer | 12 | $1.08 |
| Sol reviewer | 24 | $3.60 |
| Sonnet reviewer | 24 | $3.60 |

The worst-case selected pairing is 48 calls and $31.20 when Astra writes and
Sol or Sonnet reviews. Two such pairing ledgers reserve 96 calls and $62.40.
No single ledger enforces the aggregate allowance across other artifact
directories.

No paid comparison has been approved or run. Recheck the model catalog,
endpoint, provider and rates in `evals/stories/models.json` before proposing a
run. In particular, the Google endpoint's catalog status is recorded without
interpreting it. Do not substitute another provider or service tier under the
same approval.

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
  --max-calls 2 --max-cost-usd 0.46 --approve-paid-calls
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
Reviewer calibration has six supported references and six single-fault drafts.
Any false accept, false reject, malformed review, incomplete run, or missing
fixture repeat fails its reference gate. Reference labels are still pending
human confirmation, so this gate alone cannot select a reviewer.

The API cache is disabled so repeated requests remain real repeats. Artifacts
contain the complete visible response text, an allowlisted routing-metadata
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
