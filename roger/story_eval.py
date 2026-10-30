"""Offline planning and explicitly budgeted model evaluation for Curated stories."""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import APIStatusError, AsyncOpenAI

from roger import curated_protocol
from roger.brains import stories
from roger.brains.curated import DraftError

_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CASES = _ROOT / "evals/stories/cases.json"
DEFAULT_MODELS = _ROOT / "evals/stories/models.json"
_REQUEST_FRAMING_BYTES = 2_048
_INPUT_TOKEN_RESERVE = 32_768
_visible_response = curated_protocol._visible_response
_response_status = curated_protocol._response_status
_usage = curated_protocol._usage


class EvaluationError(RuntimeError):
    """A local evaluation invariant failed."""


@dataclasses.dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    returned_models: tuple[str, ...]
    provider: str
    service_tier: str
    prompt_usd_per_million: Decimal
    completion_usd_per_million: Decimal
    prompt_cache: str
    prompt_cache_write_usd_per_million: Decimal
    reasoning_effort: str
    writer_max_tokens: int
    reviewer_max_tokens: int
    available: bool
    note: str
    provider_display_name: str

    @classmethod
    def from_record(cls, name: str, record: dict[str, Any]) -> ModelProfile:
        pricing = record["pricing_usd_per_million"]
        try:
            prompt_cache = record["prompt_cache"]
            cache_write_price = Decimal(str(pricing["prompt_cache_write"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise EvaluationError(f"missing or invalid prompt-cache profile: {name}") from exc
        profile = cls(
            name=name,
            model=record["model"],
            returned_models=tuple(record["returned_models"]),
            provider=record["provider"],
            service_tier=record["service_tier"],
            prompt_usd_per_million=Decimal(str(pricing["prompt"])),
            completion_usd_per_million=Decimal(str(pricing["completion"])),
            prompt_cache=prompt_cache,
            prompt_cache_write_usd_per_million=cache_write_price,
            reasoning_effort=record["reasoning_effort"],
            writer_max_tokens=int(record["writer_max_tokens"]),
            reviewer_max_tokens=int(record["reviewer_max_tokens"]),
            available=bool(record["available"]),
            note=str(record.get("note") or ""),
            provider_display_name=record["provider_display_name"],
        )
        if (
            not profile.model or not profile.returned_models or not profile.provider
            or profile.service_tier != "default"
            or profile.reasoning_effort != "medium"
            or profile.writer_max_tokens <= 0 or profile.reviewer_max_tokens <= 0
            or profile.prompt_cache not in ("disabled", "provider_default")
            or (profile.prompt_cache == "disabled" and profile.provider != "openai")
            or any(
                not price.is_finite() or price <= 0
                for price in (
                    profile.prompt_usd_per_million, profile.completion_usd_per_million,
                    profile.prompt_cache_write_usd_per_million,
                )
            )
        ):
            raise EvaluationError(f"invalid model profile: {name}")
        return profile


@dataclasses.dataclass(frozen=True)
class CallSpec:
    stage: str
    case_id: str
    repeat: int
    profile: ModelProfile
    messages: list[dict[str, str]]
    response_format: dict[str, Any]
    max_tokens: int

    @property
    def request_id(self) -> str:
        raw = f"{self.stage}:{self.profile.name}:{self.case_id}:{self.repeat}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def input_token_upper_bound(self) -> int:
        content = json.dumps(
            {"messages": self.messages, "response_format": self.response_format},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # One token per UTF-8 byte plus fixed chat framing is deliberately conservative.
        return len(content) + _REQUEST_FRAMING_BYTES

    @property
    def reserved_cost_usd(self) -> Decimal:
        if self.input_token_upper_bound > _INPUT_TOKEN_RESERVE:
            raise EvaluationError(
                f"request needs {self.input_token_upper_bound} input tokens, above the "
                f"{_INPUT_TOKEN_RESERVE} token reservation"
            )
        # Cover possible billed writes even when the request asks the provider not to cache.
        prompt = Decimal(_INPUT_TOKEN_RESERVE) * max(
            self.profile.prompt_usd_per_million, self.profile.prompt_cache_write_usd_per_million
        )
        completion = Decimal(self.max_tokens) * self.profile.completion_usd_per_million
        estimated = (prompt + completion) / Decimal(1_000_000)
        return estimated.quantize(Decimal("0.01"), rounding="ROUND_CEILING")

    def inventory(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "stage": self.stage,
            "case_id": self.case_id,
            "repeat": self.repeat,
            "profile": self.profile.name,
            "model": self.profile.model,
            "returned_models": list(self.profile.returned_models),
            "provider": self.profile.provider,
            "provider_display_name": self.profile.provider_display_name,
            "service_tier": self.profile.service_tier,
            "reasoning_effort": self.profile.reasoning_effort,
            "response_cache": "disabled",
            "prompt_cache": self.profile.prompt_cache,
            "prompt_cache_write_usd_per_million": str(
                self.profile.prompt_cache_write_usd_per_million
            ),
            "max_tokens": self.max_tokens,
            "timeout_seconds": 900 if "reviewer" in self.stage else 1200,
            "input_token_upper_bound": self.input_token_upper_bound,
            "input_token_reserve": _INPUT_TOKEN_RESERVE,
            "reserved_cost_usd": str(self.reserved_cost_usd),
            "available": self.profile.available,
        }


class Ledger:
    """A durable reservation ledger for one immutable run directory."""

    def __init__(self, path: pathlib.Path, *, max_calls: int, max_cost_usd: Decimal) -> None:
        if (
            type(max_calls) is not int or max_calls <= 0
            or not max_cost_usd.is_finite() or max_cost_usd <= 0
        ):
            raise EvaluationError("run budgets must be positive and finite")
        self.path = path
        self.data: dict[str, Any] = {
            "max_calls": max_calls,
            "max_cost_usd": str(max_cost_usd),
            "reserved_calls": 0,
            "reserved_cost_usd": "0",
            "used_calls": 0,
            "used_cost_usd": "0",
            "calls": [],
        }
        self._write()

    def _write(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(self.data, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def reserve(self, spec: CallSpec) -> int:
        calls = int(self.data["reserved_calls"])
        cost = Decimal(self.data["reserved_cost_usd"])
        if calls + 1 > int(self.data["max_calls"]):
            raise EvaluationError("call budget would be exceeded")
        if cost + spec.reserved_cost_usd > Decimal(self.data["max_cost_usd"]):
            raise EvaluationError("cost budget would be exceeded")
        row = {
            **spec.inventory(),
            "status": "reserved",
            "actual_cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "latency_ms": None,
        }
        self.data["calls"].append(row)
        self.data["reserved_calls"] = calls + 1
        self.data["reserved_cost_usd"] = str(cost + spec.reserved_cost_usd)
        self._write()
        return len(self.data["calls"]) - 1

    def finish(
        self,
        index: int,
        *,
        status: str,
        usage: dict[str, int | Decimal | None] | None = None,
        latency_ms: int | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = self.data["calls"][index]
        row["status"] = status
        row["latency_ms"] = latency_ms
        if error:
            row["error"] = error
        if usage is not None:
            actual = Decimal(usage["cost_usd"])
            row.update(
                actual_cost_usd=str(actual),
                input_tokens=int(usage["input_tokens"]),
                output_tokens=int(usage["output_tokens"]),
                reasoning_tokens=usage["reasoning_tokens"],
            )
            self.data["used_calls"] = int(self.data["used_calls"]) + 1
            self.data["used_cost_usd"] = str(Decimal(self.data["used_cost_usd"]) + actual)
            if actual > Decimal(row["reserved_cost_usd"]):
                row["status"] = "overspend"
        self._write()


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot load {path}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{path} must contain a JSON object")
    return value


def load_profiles(path: pathlib.Path = DEFAULT_MODELS) -> dict[str, ModelProfile]:
    raw = _load_json(path)
    return {
        name: ModelProfile.from_record(name, record)
        for name, record in raw.get("profiles", {}).items()
    }


def load_fixtures(path: pathlib.Path = DEFAULT_CASES) -> dict[str, Any]:
    fixtures = _load_json(path)
    if fixtures.get("label_status") != "author_expected_pending_human_review":
        raise EvaluationError("fixture labels must be marked pending human review")
    return fixtures


def _case_sources(case: dict[str, Any]) -> list[dict[str, Any]]:
    return stories.evidence_bundle(
        case["entries"], case["history"],
        now=_case_now(case), max_age_hours=case["max_age_hours"]
    )


def _case_now(case: dict[str, Any]) -> datetime.datetime:
    try:
        now = datetime.datetime.fromisoformat(case["now"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvaluationError(f"case {case.get('id')} has an invalid now") from exc
    if now.tzinfo is None:
        raise EvaluationError(f"case {case.get('id')} now must be timezone-aware")
    return now.astimezone(datetime.UTC)


def _fixture_decision(fixture: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    decision = copy.deepcopy(case["reference_decision"])
    seed = fixture["seed"]
    if seed == "none":
        return decision
    if seed == "unsupported_claim":
        decision["claims"][2]["text"] = "Atlas 2.0 is 40 percent faster than every competitor."
    elif seed == "dropped_qualifier":
        decision["claims"][2]["text"] = "Northstar is twice as fast as every competing cache."
        decision["claims"][2]["citations"] = [{
            "source": 1,
            "quote": (
                "Northstar says its preview is twice as fast as every competing cache, but the "
                "post provides no benchmark data or comparison method."
            ),
        }]
    elif seed == "invented_relationship":
        decision["reason"]["text"] = "Lumen acquired Orbit to make Orbit its official client."
    elif seed == "chronology_error":
        decision["change"]["text"] = "The Windows client shipped before the Linux client."
        decision["change"]["citations"] = [{
            "source": 1,
            "quote": (
                "River's Windows client became generally available on October 8, 2026, after the "
                "Linux client shipped on October 1."
            ),
        }]
    elif seed == "stale_claim":
        stale_quote = (
            "An undated forum summary speculates that Atlas might add mobile packages someday, "
            "but it cites no maintainer statement, repository change, release artifact, or "
            "schedule."
        )
        decision = {
            "action": "update",
            "reason": {"text": "Atlas has made mobile packages available.",
                       "citations": [{"source": 2, "quote": stale_quote}]},
            "change": {"text": "Mobile support is a new material release.",
                       "citations": [{"source": 2, "quote": stale_quote}]},
            "story_ids": decision["story_ids"],
            "claims": [
                {"field": "headline", "text": "Atlas adds mobile packages",
                 "citations": [{"source": 2, "quote": stale_quote}], "mutable": False},
                {"field": "stage", "text": "rollout",
                 "citations": [{"source": 2, "quote": stale_quote}], "mutable": False},
                {"field": "fact", "text": "Mobile packages are available now.",
                 "citations": [{"source": 2, "quote": stale_quote}], "mutable": False},
                {"field": "why", "text": "The package broadens Atlas platform support.",
                 "citations": [{"source": 2, "quote": stale_quote}], "mutable": False},
            ],
            "unresolved_questions": [],
            "correction_of": None,
        }
    elif seed == "incorrect_correction":
        decision["change"]["text"] = "The active-project limit is 160 rather than 100."
    else:
        raise EvaluationError(f"unknown reviewer seed: {seed}")
    return decision


def _writer_spec(case: dict[str, Any], profile: ModelProfile, repeat: int) -> CallSpec:
    sources = _case_sources(case)
    model_input = stories._model_input(sources, case["history"])
    return CallSpec(
        stage="writer",
        case_id=case["id"],
        repeat=repeat,
        profile=profile,
        messages=stories._messages(
            stories.SYSTEM, model_input, stories.DECISION_RESPONSE_FORMAT
        ),
        response_format=stories.DECISION_RESPONSE_FORMAT,
        max_tokens=profile.writer_max_tokens,
    )


def _reviewer_spec(
    fixture: dict[str, Any], cases: dict[str, dict[str, Any]], profile: ModelProfile, repeat: int
) -> CallSpec:
    case = cases[fixture["case_id"]]
    sources = _case_sources(case)
    decision = stories.parse_decision(
        json.dumps(_fixture_decision(fixture, case)), sources, case["history"]
    )
    return CallSpec(
        stage="reviewer",
        case_id=fixture["id"],
        repeat=repeat,
        profile=profile,
        messages=stories._messages(
            stories.REVIEW,
            stories.review_payload(decision, sources, case["history"]),
            stories.REVIEW_RESPONSE_FORMAT,
        ),
        response_format=stories.REVIEW_RESPONSE_FORMAT,
        max_tokens=profile.reviewer_max_tokens,
    )


def build_plan(
    fixtures_path: pathlib.Path = DEFAULT_CASES,
    models_path: pathlib.Path = DEFAULT_MODELS,
) -> dict[str, Any]:
    fixtures = load_fixtures(fixtures_path)
    profiles = load_profiles(models_path)
    cases = {case["id"]: case for case in fixtures["cases"]}
    writer_names = fixtures["matrix"]["writer_profiles"]
    reviewer_names = fixtures["matrix"]["reviewer_profiles"]
    repeats = int(fixtures["matrix"]["repeats"])
    writer = [
        _writer_spec(case, profiles[name], repeat).inventory()
        for name in writer_names
        for case in fixtures["cases"]
        for repeat in range(1, repeats + 1)
    ]
    reviewer = [
        _reviewer_spec(fixture, cases, profiles[name], repeat).inventory()
        for name in reviewer_names
        for fixture in fixtures["reviewer_fixtures"]
        for repeat in range(1, repeats + 1)
    ]

    def total(rows: list[dict[str, Any]]) -> str:
        return str(sum((Decimal(row["reserved_cost_usd"]) for row in rows), Decimal()))

    return {
        "network_used": False,
        "credentials_required": False,
        "label_status": fixtures["label_status"],
        "writer_screen": {
            "calls": len(writer),
            "cost_upper_bound_usd": total(writer),
            "inventory": writer,
        },
        "reviewer_calibration": {
            "calls": len(reviewer),
            "cost_upper_bound_usd": total(reviewer),
            "inventory": reviewer,
            "reference_gate": (
                "zero false accepts and zero false rejects; human confirmation still required"
            ),
        },
        "selected_pair_workflow": {
            "cases": len(fixtures["cases"]),
            "repeats": repeats,
            "workflow_runs_per_pair": len(fixtures["cases"]) * repeats,
            "maximum_calls_per_workflow": 4,
            "maximum_calls_per_pair": len(fixtures["cases"]) * repeats * 4,
            "maximum_calls_for_two_pairings": len(fixtures["cases"]) * repeats * 8,
            "priced_after_writer_and_reviewer_selection": True,
        },
        "notes": _load_json(models_path).get("notes", []),
    }


def _request_kwargs(spec: CallSpec) -> dict[str, Any]:
    try:
        return curated_protocol.request_kwargs(
            spec.profile, spec.messages, spec.response_format, max_tokens=spec.max_tokens,
            timeout=900.0 if "reviewer" in spec.stage else 1200.0,
        )
    except ValueError as exc:
        raise EvaluationError(str(exc)) from exc


def _request_record(spec: CallSpec) -> dict[str, Any]:
    kwargs = _request_kwargs(spec)
    return {
        **spec.inventory(),
        "messages": kwargs["messages"],
        "response_format": kwargs["response_format"],
        "parameters": {
            key: value for key, value in kwargs.items()
            if key not in {"messages", "response_format", "timeout"}
        },
    }


def _safe_error(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"class": type(exc).__name__}
    if isinstance(exc, APIStatusError):
        result["status_code"] = exc.status_code
    return result


class Runner:
    def __init__(self, client: AsyncOpenAI, ledger: Ledger, responses: pathlib.Path) -> None:
        self.client = client
        self.ledger = ledger
        self.responses = responses
        self.call_count = 0

    async def call(self, spec: CallSpec) -> tuple[Any, dict[str, Any]]:
        if not spec.profile.available:
            raise EvaluationError(
                f"profile {spec.profile.name} is marked unavailable: {spec.profile.note}"
            )
        kwargs = _request_kwargs(spec)
        index = self.ledger.reserve(spec)
        started = time.monotonic()
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            latency = round((time.monotonic() - started) * 1000)
            self.ledger.finish(
                index, status="request_error_uncertain", latency_ms=latency,
                error=_safe_error(exc),
            )
            raise EvaluationError(f"request failed: {type(exc).__name__}") from exc
        latency = round((time.monotonic() - started) * 1000)
        visible = _visible_response(response)
        usage = _usage(response)
        status = _response_status(visible, spec.profile)
        cache_counts = [visible["usage_details"][key] for key in (
            "cached_tokens", "cache_write_tokens"
        )]
        visible["prompt_cache_observation"] = (
            "activity_reported" if any(count is not None and count > 0 for count in cache_counts)
            else "none_reported" if cache_counts == [0, 0] else "unknown"
        )
        if (
            status == "completed" and spec.profile.prompt_cache == "disabled"
            and visible["prompt_cache_observation"] == "activity_reported"
        ):
            status = "prompt_cache_policy_mismatch"
        if usage is None and status != "malformed_response":
            status = "missing_usage_or_cost"
        elif usage is not None and (
            usage["input_tokens"] > _INPUT_TOKEN_RESERVE
            or usage["output_tokens"] > spec.max_tokens
            or (
                usage["reasoning_tokens"] is not None
                and usage["reasoning_tokens"] > usage["output_tokens"]
            )
        ):
            status = "invalid_usage_accounting"
        self.ledger.finish(index, status=status, usage=usage, latency_ms=latency)
        artifact = {
            "request": _request_record(spec),
            "response": visible,
            "accounting": usage and {
                key: str(value) if value is not None else None for key, value in usage.items()
            },
            "status": self.ledger.data["calls"][index]["status"],
        }
        destination = self.responses / f"{index + 1:03d}-{spec.request_id}.json"
        destination.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
        self.call_count += 1
        if self.ledger.data["calls"][index]["status"] != "completed":
            raise EvaluationError(f"call stopped: {self.ledger.data['calls'][index]['status']}")
        return response, visible


class WorkflowLLM:
    """The narrow adapter needed by the production ``stories.decide`` workflow."""

    def __init__(
        self,
        runner: Runner,
        writer: ModelProfile,
        reviewer: ModelProfile,
        case_id: str,
        repeat: int,
    ) -> None:
        self.runner = runner
        self.writer = writer
        self.reviewer = reviewer
        self.case_id = case_id
        self.repeat = repeat
        self.first_call = runner.call_count

    async def complete(
        self,
        brain: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        *,
        curated_review: bool = False,
        curated_story: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        if brain != "curated" or tools or response_format is None:
            raise EvaluationError("unexpected production workflow request")
        if self.runner.call_count - self.first_call >= 4:
            raise EvaluationError("production workflow exceeded four calls")
        profile = self.reviewer if curated_review else self.writer
        stage = "workflow-reviewer" if curated_review else "workflow-writer"
        max_tokens = profile.reviewer_max_tokens if curated_review else profile.writer_max_tokens
        spec = CallSpec(
            stage=stage,
            case_id=self.case_id,
            repeat=self.repeat,
            profile=profile,
            messages=messages,
            response_format=response_format,
            max_tokens=max_tokens,
        )
        response, _ = await self.runner.call(spec)
        return response


def _create_run_dir(path: pathlib.Path) -> pathlib.Path:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise EvaluationError("artifact directory already exists; runs cannot be resumed") from exc
    responses = path / "responses"
    responses.mkdir()
    return responses


def _snapshot_inputs(
    artifact_dir: pathlib.Path,
    fixtures_path: pathlib.Path,
    models_path: pathlib.Path,
    fixtures: dict[str, Any],
) -> None:
    fixtures_bytes = fixtures_path.read_bytes()
    models_bytes = models_path.read_bytes()
    (artifact_dir / "inputs.json").write_text(json.dumps({
        "fixtures_path": str(fixtures_path),
        "fixtures_sha256": hashlib.sha256(fixtures_bytes).hexdigest(),
        "fixtures": fixtures,
        "models_path": str(models_path),
        "models_sha256": hashlib.sha256(models_bytes).hexdigest(),
        "models": _load_json(models_path),
    }, indent=2) + "\n", encoding="utf-8")


def _validate_decision_output(
    visible: dict[str, Any], case: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    try:
        decision = stories.parse_decision(
            visible["content"], _case_sources(case), case["history"]
        )
    except DraftError as exc:
        return "local_parse_failure", {"class": type(exc).__name__, "code": str(exc)}
    return "local_parse_pass", decision.record()


def _source_inventory(case: dict[str, Any]) -> list[dict[str, Any]]:
    prepared = _case_sources(case)
    by_identity = {}
    for source in prepared:
        identity = (source.get("feed_url"), source.get("entry_id"), source.get("url"))
        by_identity.setdefault(identity, source)
    inventory = []
    for entry in case["entries"]:
        article = entry.get("article") or {}
        text = str(article.get("text") or "")
        identity = (entry.get("feed_url"), entry.get("id"), article.get("url"))
        source = by_identity.get(identity)
        delivered_chars = len(source["excerpt"]) if source else 0
        inventory.append({
            "url": article.get("url"),
            "source_admitted": source is not None,
            "original_chars": len(text),
            "delivered_excerpt_chars": delivered_chars,
            "omitted_chars": max(0, len(text) - delivered_chars),
        })
    return inventory


def _review_verdict(
    visible: dict[str, Any], fixture: dict[str, Any], case: dict[str, Any]
) -> dict[str, Any]:
    expected_accept = fixture["expected"]["review_accept"]

    def invalid(error_class: str) -> dict[str, Any]:
        return {
            "status": "local_parse_failure",
            "error_class": error_class,
            "expected_label_pending_human_review": expected_accept,
            "matches_expected": False,
        }

    if (
        not isinstance(visible.get("content"), str)
    ):
        return invalid("InvalidReviewContent")
    sources = _case_sources(case)
    decision = stories.parse_decision(
        json.dumps(_fixture_decision(fixture, case)), sources, case["history"]
    )
    try:
        accepted, verdict, failed = stories.parse_review(
            visible["content"], decision, sources,
        )
    except DraftError as exc:
        return invalid(str(exc))
    return {
        "status": "reviewer_accept" if accepted else "reviewer_reject",
        "expected_label_pending_human_review": expected_accept,
        "matches_expected": accepted is expected_accept,
        "failed_gates": failed,
        "verdict": verdict,
    }


async def run_matrix(
    *,
    stage: str,
    artifact_dir: pathlib.Path,
    max_calls: int,
    max_cost_usd: Decimal,
    profile_name: str | None,
    writer_name: str | None,
    reviewer_name: str | None,
    case_ids: list[str],
    repeats: int,
    fixtures_path: pathlib.Path = DEFAULT_CASES,
    models_path: pathlib.Path = DEFAULT_MODELS,
    client: AsyncOpenAI | None = None,
) -> dict[str, Any]:
    fixtures = load_fixtures(fixtures_path)
    profiles = load_profiles(models_path)
    cases = {case["id"]: case for case in fixtures["cases"]}
    selected = case_ids or list(cases)
    if stage != "reviewer" and (unknown := sorted(set(selected) - set(cases))):
        raise EvaluationError(f"unknown cases: {', '.join(unknown)}")
    if stage in {"writer", "reviewer"} and profile_name not in profiles:
        raise EvaluationError(f"{stage} run requires a known --model")
    if stage == "workflow" and (writer_name not in profiles or reviewer_name not in profiles):
        raise EvaluationError("workflow run requires known --writer-model and --reviewer-model")
    owns_client = client is None
    if owns_client:
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise EvaluationError("OPENROUTER_API_KEY is required only for run")
    responses = _create_run_dir(artifact_dir)
    _snapshot_inputs(artifact_dir, fixtures_path, models_path, fixtures)
    ledger = Ledger(
        artifact_dir / "ledger.json", max_calls=max_calls, max_cost_usd=max_cost_usd
    )
    if owns_client:
        client = AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            max_retries=0,
            timeout=1200.0,
            default_headers=curated_protocol.PROFILE_HEADERS,
        )
    runner = Runner(client, ledger, responses)
    results: list[dict[str, Any]] = []

    def persist(run_status: str, error: Exception | None = None) -> dict[str, Any]:
        summary = {
            "label_status": fixtures["label_status"],
            "stage": stage,
            "run_status": run_status,
            "results": results,
            "human_rubric": fixtures["human_rubric"],
            "accounting": {
                key: ledger.data[key]
                for key in (
                    "max_calls", "max_cost_usd", "reserved_calls", "reserved_cost_usd",
                    "used_calls", "used_cost_usd",
                )
            },
            "human_acceptance": "pending",
        }
        if error is not None:
            summary["terminal_error"] = _safe_error(error)
        (artifact_dir / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return summary

    persist("running")
    try:
        if stage == "writer":
            for case_id in selected:
                for repeat in range(1, repeats + 1):
                    case = cases[case_id]
                    _, visible = await runner.call(
                        _writer_spec(case, profiles[profile_name], repeat)
                    )
                    status, decision = _validate_decision_output(visible, case)
                    results.append(
                        {"case_id": case_id, "repeat": repeat, "status": status,
                         "decision": decision,
                         "reference_expected": case["expected"],
                         "false_hold_on_reportable": bool(
                             status == "local_parse_pass" and case["expected"]["reportable"]
                             and decision["action"] in {"hold", "skip"}
                         ),
                         "source_inventory": _source_inventory(case),
                         "human_rating": None}
                    )
                    persist("running")
        elif stage == "reviewer":
            review_fixtures = {
                fixture["id"]: fixture for fixture in fixtures["reviewer_fixtures"]
            }
            selected_fixtures = case_ids or list(review_fixtures)
            if unknown := sorted(set(selected_fixtures) - set(review_fixtures)):
                raise EvaluationError(f"unknown reviewer fixtures: {', '.join(unknown)}")
            for fixture_id in selected_fixtures:
                for repeat in range(1, repeats + 1):
                    fixture = review_fixtures[fixture_id]
                    _, visible = await runner.call(
                        _reviewer_spec(fixture, cases, profiles[profile_name], repeat)
                    )
                    results.append({"fixture_id": fixture_id, "repeat": repeat,
                                    **_review_verdict(visible, fixture, cases[fixture["case_id"]])})
                    persist("running")
        else:
            for case_id in selected:
                for repeat in range(1, repeats + 1):
                    before = runner.call_count
                    llm = WorkflowLLM(
                        runner, profiles[writer_name], profiles[reviewer_name], case_id, repeat
                    )
                    try:
                        decision, _ = await stories.decide(
                            cases[case_id]["entries"], cases[case_id]["history"], llm,
                            now=_case_now(cases[case_id]),
                            max_age_hours=cases[case_id]["max_age_hours"],
                        )
                    except DraftError as exc:
                        result = {"status": "workflow_rejected", "error_class": type(exc).__name__}
                    else:
                        result = {"status": "workflow_accepted", "decision": decision.record()}
                    results.append(
                        {"case_id": case_id, "repeat": repeat,
                         "calls": runner.call_count - before,
                         "reference_expected": cases[case_id]["expected"],
                         "source_inventory": _source_inventory(cases[case_id]),
                         "human_rating": None, **result}
                    )
                    persist("running")
    except Exception as exc:
        persist("stopped", exc)
        raise
    finally:
        if owns_client and client is not None and hasattr(client, "close"):
            await client.close()
    return persist("complete")


def summarize(path: pathlib.Path) -> dict[str, Any]:
    ledger = _load_json(path / "ledger.json")
    summary_path = path / "summary.json"
    result = _load_json(summary_path) if summary_path.exists() else {}
    statuses: dict[str, int] = {}
    for row in ledger.get("calls", []):
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    reviewer_rows = [
        row for row in result.get("results", [])
        if "expected_label_pending_human_review" in row
    ]
    expected_matches = [row["matches_expected"] for row in reviewer_rows]
    confusion = {
        "expected_accept": 0,
        "expected_reject": 0,
        "false_accept": 0,
        "false_reject": 0,
    }
    for row in reviewer_rows:
        expected = row["expected_label_pending_human_review"]
        actual = row["status"] == "reviewer_accept"
        confusion["expected_accept" if expected else "expected_reject"] += 1
        if actual and not expected:
            confusion["false_accept"] += 1
        if expected and not actual:
            confusion["false_reject"] += 1
    invalid_reviews = sum(row["status"] == "local_parse_failure" for row in reviewer_rows)
    full_coverage = False
    inputs_path = path / "inputs.json"
    if result.get("stage") == "reviewer" and inputs_path.exists():
        inputs = _load_json(inputs_path)
        fixture_data = inputs.get("fixtures") or {}
        expected_repeats = (fixture_data.get("matrix") or {}).get("repeats")
        expected_ids = [
            fixture.get("id") for fixture in fixture_data.get("reviewer_fixtures", [])
            if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
        ]
        actual_counts = {
            fixture_id: sum(row.get("fixture_id") == fixture_id for row in reviewer_rows)
            for fixture_id in expected_ids
        }
        full_coverage = (
            type(expected_repeats) is int
            and expected_repeats > 0
            and bool(expected_ids)
            and len(reviewer_rows) == len(expected_ids) * expected_repeats
            and all(count == expected_repeats for count in actual_counts.values())
        )
    completed_run = (
        result.get("run_status") == "complete"
        and statuses.get("completed", 0) == len(reviewer_rows)
        and ledger.get("reserved_calls") == len(reviewer_rows)
    )
    reference_gate_pass = (
        completed_run
        and full_coverage
        and invalid_reviews == 0
        and confusion["false_accept"] == 0
        and confusion["false_reject"] == 0
    )
    return {
        "calls_by_status": statuses,
        "accounting": {
            key: ledger.get(key) for key in (
                "max_calls", "max_cost_usd", "reserved_calls", "reserved_cost_usd",
                "used_calls", "used_cost_usd",
            )
        },
        "reviewer_expected_matches": {
            "matched": sum(expected_matches), "total": len(expected_matches),
            "labels": "author expected, pending human review",
            **confusion,
            "invalid_reviews": invalid_reviews,
            "completed_run": completed_run,
            "full_fixture_coverage": full_coverage,
            "reference_gate_pass": reference_gate_pass,
            "selection_authorized": False,
        },
        "human_acceptance": "pending",
    }


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    plan = sub.add_parser("plan", help="print the network-free priced request inventory")
    plan.add_argument("--fixtures", type=pathlib.Path, default=DEFAULT_CASES)
    plan.add_argument("--models", type=pathlib.Path, default=DEFAULT_MODELS)
    run = sub.add_parser("run", help="perform an explicitly approved paid evaluation run")
    run.add_argument("--stage", choices=("writer", "reviewer", "workflow"), required=True)
    run.add_argument("--artifact-dir", type=pathlib.Path, required=True)
    run.add_argument("--max-calls", type=int, required=True)
    run.add_argument("--max-cost-usd", type=_decimal, required=True)
    run.add_argument("--approve-paid-calls", action="store_true", required=True)
    run.add_argument("--model")
    run.add_argument("--writer-model")
    run.add_argument("--reviewer-model")
    run.add_argument("--case", action="append", default=[])
    run.add_argument("--repeats", type=int, default=2)
    run.add_argument("--fixtures", type=pathlib.Path, default=DEFAULT_CASES)
    run.add_argument("--models", type=pathlib.Path, default=DEFAULT_MODELS)
    report = sub.add_parser("summarize", help="summarize local artifacts without model calls")
    report.add_argument("artifact_dir", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "plan"
    try:
        if command == "plan":
            output = build_plan(
                getattr(args, "fixtures", DEFAULT_CASES), getattr(args, "models", DEFAULT_MODELS)
            )
        elif command == "summarize":
            output = summarize(args.artifact_dir)
        else:
            if args.repeats <= 0:
                raise EvaluationError("repeats must be positive")
            output = asyncio.run(
                run_matrix(
                    stage=args.stage,
                    artifact_dir=args.artifact_dir,
                    max_calls=args.max_calls,
                    max_cost_usd=args.max_cost_usd,
                    profile_name=args.model,
                    writer_name=args.writer_model,
                    reviewer_name=args.reviewer_model,
                    case_ids=args.case,
                    repeats=args.repeats,
                    fixtures_path=args.fixtures,
                    models_path=args.models,
                )
            )
    except EvaluationError as exc:
        print(f"story evaluation stopped: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
