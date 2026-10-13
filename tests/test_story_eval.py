import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from roger import story_eval
from roger.brains import stories


def _completion(
    content,
    *,
    model="openai/gpt-6.1-sol",
    cost=0.01,
    reasoning_tokens=11,
    finish_reason="stop",
    provider="OpenAI",
    include_usage=True,
    pipeline=None,
    service_tier="default",
):
    payload = {
        "id": "eval-response",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "service_tier": service_tier,
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {"role": "assistant", "content": content},
        }],
        "openrouter_metadata": {
            "requested": "openai/gpt-6.1-sol-20260929",
            "strategy": "only",
            "attempt": 1,
            "attempts": [{"provider": provider}],
            "endpoints": {"available": [{
                "provider": provider, "model": model, "selected": True,
            }]},
            "pipeline": pipeline or [],
        },
    }
    if include_usage:
        payload["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "total_tokens": 125,
            "cost": float(cost),
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }
    return payload


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=content, refusal=None),
        )]
    )


def _passed_moderation():
    return {
        "type": "guardrail",
        "name": "moderation",
        "summary": "Content passed OpenAI moderation",
        "data": {
            "engine": "openai-moderations",
            "flagged": False,
            "moderator_ids": ["OpenAIOmniModerator"],
        },
    }


def _review(accepted=True, item_count=6):
    return json.dumps({
        "supported": [accepted, *([True] * (item_count - 1))],
        "material_change": True,
        "action_supported": True,
        "chronology_supported": True,
        "relationship_supported": True,
        "freshness_supported": True,
        "correction_supported": True,
    })


def _fixture_data():
    fixtures = story_eval.load_fixtures()
    cases = {case["id"]: case for case in fixtures["cases"]}
    reviews = {fixture["id"]: fixture for fixture in fixtures["reviewer_fixtures"]}
    return fixtures, cases, reviews


def test_fixture_reference_decisions_and_seeded_reviews_use_delivered_evidence():
    fixtures, cases, _ = _fixture_data()
    assert [case["id"] for case in fixtures["cases"]] == [
        "straight_release",
        "skeptical_marketing",
        "material_update",
        "correction",
        "legitimate_combine",
        "duplicate_stale_hold",
    ]
    assert all(case["now"].endswith("+00:00") for case in fixtures["cases"])
    for case in fixtures["cases"]:
        sources = story_eval._case_sources(case)
        assert sources
        assert all(source["url"].endswith(".test/" + source["url"].rsplit("/", 1)[-1])
                   for source in sources)
        assert all(len(entry["article"]["text"]) >= 350 for entry in case["entries"])
        current_ids = {entry["id"] for entry in case["entries"]}
        for source in sources:
            if source.get("entry_id") in current_ids:
                assert source["excerpt"] in next(
                    entry["article"]["text"] for entry in case["entries"]
                    if entry["article"]["url"] == source["url"]
                )
        decision = stories.parse_decision(
            json.dumps(case["reference_decision"]), sources, case["history"]
        )
        assert decision.action == case["expected"]["action"]
        for value in (decision.reason, decision.change, *decision.claims):
            for citation in value["citations"]:
                assert citation["quote"] in sources[citation["source"] - 1]["excerpt"]

    for fixture in fixtures["reviewer_fixtures"]:
        case = cases[fixture["case_id"]]
        stories.parse_decision(
            json.dumps(story_eval._fixture_decision(fixture, case)),
            story_eval._case_sources(case),
            case["history"],
        )


def test_fixture_history_targets_are_confirmed_and_first_four():
    _, cases, _ = _fixture_data()
    assert cases["material_update"]["history"]["delivered"][0]["id"] == 301
    correction = cases["correction"]
    assert correction["reference_decision"]["correction_of"] == 401
    assert correction["history"]["delivered"][0]["id"] == 401
    for case in cases.values():
        for delivered in case["history"]["delivered"]:
            assert delivered["message_id"].startswith("example-message-")
            assert delivered["message_url"].startswith("https://discord.example.invalid/")


def test_source_inventory_matches_current_versions_and_reports_real_truncation():
    _, cases, _ = _fixture_data()
    for case_id in ("material_update", "correction", "duplicate_stale_hold"):
        inventory = story_eval._source_inventory(cases[case_id])
        assert all(row["omitted_chars"] == 0 for row in inventory)
        assert all(
            row["delivered_excerpt_chars"] == row["original_chars"]
            for row in inventory
        )

    long_case = json.loads(json.dumps(cases["straight_release"]))
    long_case["entries"][0]["article"]["text"] = "x" * 1700
    inventory = story_eval._source_inventory(long_case)
    assert inventory[0]["original_chars"] == 1700
    assert inventory[0]["delivered_excerpt_chars"] == 1600
    assert inventory[0]["omitted_chars"] == 100


def test_offline_plan_does_not_construct_client(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("plan constructed a client")

    monkeypatch.setattr(story_eval, "AsyncOpenAI", forbidden)
    plan = story_eval.build_plan()
    assert plan["network_used"] is False
    assert plan["credentials_required"] is False
    assert plan["writer_screen"]["calls"] == 48
    assert plan["reviewer_calibration"]["calls"] == 48
    assert plan["writer_screen"]["cost_upper_bound_usd"] == "20.40"
    assert plan["reviewer_calibration"]["cost_upper_bound_usd"] == "7.20"
    assert plan["selected_pair_workflow"]["maximum_calls_for_two_pairings"] == 96


def test_input_reservation_counts_utf8_bytes_and_full_schema():
    _, cases, _ = _fixture_data()
    profile = story_eval.load_profiles()["sol"]
    spec = story_eval._writer_spec(cases["straight_release"], profile, 1)
    replacement = story_eval.CallSpec(
        **{**spec.__dict__, "messages": [*spec.messages, {"role": "user", "content": "é"}]}
    )
    ascii_replacement = story_eval.CallSpec(
        **{**spec.__dict__, "messages": [*spec.messages, {"role": "user", "content": "e"}]}
    )
    assert replacement.input_token_upper_bound == ascii_replacement.input_token_upper_bound + 1
    assert spec.inventory()["input_token_reserve"] == 32768


@pytest.mark.asyncio
async def test_sdk_request_is_exactly_pinned_and_has_no_retry_or_sampling(tmp_path):
    _, cases, _ = _fixture_data()
    profile = story_eval.load_profiles()["sol"]
    spec = story_eval._writer_spec(cases["straight_release"], profile, 1)
    requests = []

    async def handle(request):
        requests.append(request)
        content = json.dumps(cases["straight_release"]["reference_decision"])
        return httpx.Response(200, json=_completion(content))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1",
        max_retries=0, http_client=http_client,
        default_headers={"X-OpenRouter-Metadata": "enabled", "X-OpenRouter-Cache": "false"},
    )
    ledger = story_eval.Ledger(tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("1"))
    responses = tmp_path / "responses"
    responses.mkdir()
    runner = story_eval.Runner(client, ledger, responses)
    await runner.call(spec)
    await client.close()

    assert len(requests) == 1
    request = json.loads(requests[0].content)
    assert request["model"] == "openai/gpt-6.1-sol-20260929"
    assert request["service_tier"] == "default"
    assert request["max_tokens"] == 16384
    assert request["provider"] == {
        "only": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "max_price": {"prompt": 2.0, "completion": 10.0},
    }
    assert request["reasoning"] == {"effort": "medium", "exclude": True}
    assert request["response_format"]["json_schema"]["strict"] is True
    assert not {"temperature", "top_p", "models"}.intersection(request)
    assert requests[0].headers["x-openrouter-metadata"] == "enabled"
    assert requests[0].headers["x-openrouter-cache"] == "false"


def test_ledger_reserves_before_calls_and_enforces_both_limits(tmp_path):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(cases["straight_release"], story_eval.load_profiles()["sol"], 1)
    ledger = story_eval.Ledger(tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("0.23"))
    ledger.reserve(spec)
    durable = json.loads((tmp_path / "ledger.json").read_text())
    assert durable["reserved_calls"] == 1
    assert durable["reserved_cost_usd"] == "0.23"
    with pytest.raises(story_eval.EvaluationError, match="call budget"):
        ledger.reserve(spec)

    costly = story_eval.Ledger(tmp_path / "cost.json", max_calls=2, max_cost_usd=Decimal("0.22"))
    with pytest.raises(story_eval.EvaluationError, match="cost budget"):
        costly.reserve(spec)


def test_run_directory_is_exclusive(tmp_path):
    path = tmp_path / "one-run"
    story_eval._create_run_dir(path)
    with pytest.raises(story_eval.EvaluationError, match="cannot be resumed"):
        story_eval._create_run_dir(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_completion("{}", include_usage=False), "missing_usage_or_cost"),
        (_completion("{}", cost=Decimal("-0.01")), "missing_usage_or_cost"),
        (_completion("{}", cost=Decimal("2")), "overspend"),
        (_completion("{}", pipeline=["response_healing"]), "pipeline_interference"),
    ],
)
async def test_failed_accounting_and_pipeline_keep_reservation(tmp_path, response, expected):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(cases["straight_release"], story_eval.load_profiles()["sol"], 1)

    async def handle(request):
        return httpx.Response(200, json=response)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1",
        max_retries=0, http_client=http_client,
    )
    ledger = story_eval.Ledger(tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("3"))
    responses = tmp_path / "responses"
    responses.mkdir()
    with pytest.raises(story_eval.EvaluationError, match="call stopped"):
        await story_eval.Runner(client, ledger, responses).call(spec)
    await client.close()
    assert ledger.data["reserved_calls"] == 1
    assert ledger.data["calls"][0]["status"] == expected


@pytest.mark.asyncio
async def test_passed_moderation_pipeline_is_recorded_and_allowed(tmp_path):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(
        cases["straight_release"], story_eval.load_profiles()["sol"], 1
    )
    response = ChatCompletion.model_validate(
        _completion("{}", pipeline=[_passed_moderation()])
    )

    async def create(**kwargs):
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    ledger = story_eval.Ledger(
        tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("1")
    )
    responses = tmp_path / "responses"
    responses.mkdir()

    await story_eval.Runner(client, ledger, responses).call(spec)

    artifact = json.loads(next(responses.iterdir()).read_text())
    metadata = artifact["response"]["route_metadata"]
    assert artifact["status"] == "completed"
    assert metadata["pipeline_pass_only"] is True
    assert metadata["pipeline_stages"] == [
        {**_passed_moderation(), "recognized_pass_only": True}
    ]


@pytest.mark.parametrize(
    "pipeline",
    [
        [{**_passed_moderation(), "unknown": "field"}],
        [{**_passed_moderation(), "data": {**_passed_moderation()["data"], "flagged": True}}],
        [{**_passed_moderation(), "data": {**_passed_moderation()["data"], "flagged": 0}}],
        [{**_passed_moderation(), "data": {**_passed_moderation()["data"], "action": "allow"}}],
        [{**_passed_moderation(), "data": {**_passed_moderation()["data"], "redact": False}}],
        [{"type": "response_healing", "name": "response-healing", "data": {}}],
        [{"type": "context_compression", "name": "context-compression", "data": {}}],
        [{"type": "plugin", "name": "web-search", "data": {}}],
        [{"type": "server_tools", "name": "server-tools", "data": {}}],
        [{"type": "future_stage", "name": "unknown", "data": {}}],
    ],
)
def test_pipeline_rejects_modified_or_unknown_stages(pipeline):
    response = ChatCompletion.model_validate(_completion("{}", pipeline=pipeline))
    visible = story_eval._visible_response(response)

    assert visible["route_metadata"]["pipeline_pass_only"] is False
    assert story_eval._response_status(
        visible, story_eval.load_profiles()["sol"]
    ) == "pipeline_interference"


@pytest.mark.asyncio
async def test_missing_optional_reasoning_usage_stays_unknown(tmp_path):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(
        cases["straight_release"], story_eval.load_profiles()["sol"], 1
    )

    async def handle(request):
        return httpx.Response(
            200, json=_completion("{}", reasoning_tokens=None, service_tier=None)
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1",
        max_retries=0, http_client=http_client,
    )
    ledger = story_eval.Ledger(
        tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("1")
    )
    responses = tmp_path / "responses"
    responses.mkdir()
    await story_eval.Runner(client, ledger, responses).call(spec)
    await client.close()
    assert ledger.data["calls"][0]["status"] == "completed"
    assert ledger.data["calls"][0]["reasoning_tokens"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choices", "shape_error", "include_usage"),
    [
        (None, "null_choices", True),
        ([], "empty_choices", False),
        ([SimpleNamespace(finish_reason="stop")], "missing_message", True),
    ],
)
async def test_malformed_sdk_response_persists_safe_artifacts_and_accounting(
    tmp_path, choices, shape_error, include_usage
):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(
        cases["straight_release"], story_eval.load_profiles()["sol"], 1
    )
    usage = None
    if include_usage:
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            cost=0.01,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=11),
        )
    response = ChatCompletion.model_construct(
        id="eval-response",
        object="chat.completion",
        created=1,
        model="openai/gpt-6.1-sol",
        service_tier="default",
        choices=choices,
        usage=usage,
        openrouter_metadata=_completion("{}")["openrouter_metadata"],
    )
    calls = 0

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    ledger = story_eval.Ledger(
        tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("1")
    )
    responses = tmp_path / "responses"
    responses.mkdir()

    with pytest.raises(story_eval.EvaluationError, match="call stopped: malformed_response"):
        await story_eval.Runner(client, ledger, responses).call(spec)

    assert calls == 1
    durable = json.loads((tmp_path / "ledger.json").read_text())
    row = durable["calls"][0]
    assert row["status"] == "malformed_response"
    assert durable["reserved_calls"] == 1
    assert durable["used_calls"] == (1 if include_usage else 0)
    assert durable["reserved_cost_usd"] == "0.23"
    assert durable["used_cost_usd"] == ("0.01" if include_usage else "0")

    artifact = json.loads(next(responses.iterdir()).read_text())
    assert artifact["status"] == "malformed_response"
    assert artifact["response"]["shape_error"] == shape_error
    assert artifact["response"]["model"] == "openai/gpt-6.1-sol"
    assert artifact["response"]["route_metadata"]["attempt"] == 1
    assert (artifact["accounting"] is not None) is include_usage
    assert "choices" not in artifact["response"]
    assert "reasoning" not in artifact["response"]


@pytest.mark.asyncio
async def test_request_error_has_one_attempt_and_sanitized_durable_error(tmp_path):
    _, cases, _ = _fixture_data()
    spec = story_eval._writer_spec(cases["straight_release"], story_eval.load_profiles()["sol"], 1)
    attempts = 0

    async def handle(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": {"message": "sensitive upstream text"}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1",
        max_retries=0, http_client=http_client,
    )
    ledger = story_eval.Ledger(tmp_path / "ledger.json", max_calls=1, max_cost_usd=Decimal("1"))
    responses = tmp_path / "responses"
    responses.mkdir()
    with pytest.raises(story_eval.EvaluationError, match="InternalServerError"):
        await story_eval.Runner(client, ledger, responses).call(spec)
    await client.close()
    assert attempts == 1
    row = json.loads((tmp_path / "ledger.json").read_text())["calls"][0]
    assert row["status"] == "request_error_uncertain"
    assert row["error"] == {"class": "InternalServerError", "status_code": 503}
    assert "sensitive" not in json.dumps(row)


def test_reviewer_report_matches_production_validation_rules():
    _, cases, reviews = _fixture_data()
    fixture = reviews["good_release"]
    accepted = story_eval._review_verdict(
        {"content": _review()}, fixture, cases["straight_release"]
    )
    assert accepted["status"] == "reviewer_accept"
    assert accepted["matches_expected"] is True

    invalid = story_eval._review_verdict(
        {"content": _review(item_count=0)}, fixture, cases["straight_release"]
    )
    assert invalid["status"] == "local_parse_failure"
    assert invalid["expected_label_pending_human_review"] is True
    assert invalid["matches_expected"] is False

    seeded = reviews["unsupported_claim"]
    rejected = story_eval._review_verdict(
        {"content": _review(accepted=False)}, seeded, cases["straight_release"]
    )
    assert rejected["status"] == "reviewer_reject"
    assert rejected["matches_expected"] is True


class _QueueRunner:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.call_count = 0

    async def call(self, spec):
        self.call_count += 1
        response = _response(next(self.contents))
        return response, {"content": response.choices[0].message.content}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contents", "expected_calls"),
    [
        ("accepted", 2),
        ("local_revision", 3),
        ("review_revision", 4),
    ],
)
async def test_actual_production_workflow_uses_two_three_or_four_calls(contents, expected_calls):
    _, cases, _ = _fixture_data()
    case = cases["straight_release"]
    good = json.dumps(case["reference_decision"])
    if contents == "accepted":
        queue = [good, _review()]
    elif contents == "local_revision":
        queue = ["{}", good, _review()]
    else:
        queue = [good, _review(accepted=False), good, _review()]
    runner = _QueueRunner(queue)
    profiles = story_eval.load_profiles()
    llm = story_eval.WorkflowLLM(runner, profiles["sol"], profiles["sonnet"], case["id"], 1)
    decision, _ = await stories.decide(
        case["entries"], case["history"], llm,
        now=story_eval._case_now(case), max_age_hours=case["max_age_hours"],
    )
    assert decision.action == "publish"
    assert runner.call_count == expected_calls


@pytest.mark.asyncio
async def test_run_saves_visible_parse_failure_and_leaves_human_assessment_pending(tmp_path):
    async def handle(request):
        return httpx.Response(200, json=_completion("{}"))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(
        api_key="test", base_url="https://openrouter.ai/api/v1",
        max_retries=0, http_client=http_client,
    )
    artifact_dir = tmp_path / "artifacts"
    summary = await story_eval.run_matrix(
        stage="writer", artifact_dir=artifact_dir,
        max_calls=1, max_cost_usd=Decimal("1"), profile_name="sol",
        writer_name=None, reviewer_name=None,
        case_ids=["straight_release"], repeats=1, client=client,
    )
    await client.close()
    assert summary["results"][0]["status"] == "local_parse_failure"
    assert summary["human_acceptance"] == "pending"
    response = json.loads(next((artifact_dir / "responses").iterdir()).read_text())
    assert response["response"]["content"] == "{}"
    assert "reasoning" not in response["response"]


def test_summary_reports_reviewer_false_accepts_without_claiming_human_acceptance(tmp_path):
    (tmp_path / "ledger.json").write_text(json.dumps({
        "max_calls": 1, "max_cost_usd": "1", "reserved_calls": 1,
        "reserved_cost_usd": "0.15", "used_calls": 1, "used_cost_usd": "0.01",
        "calls": [{"status": "completed"}],
    }))
    (tmp_path / "summary.json").write_text(json.dumps({"results": [{
        "status": "reviewer_accept",
        "expected_label_pending_human_review": False,
        "matches_expected": False,
    }]}))
    report = story_eval.summarize(tmp_path)
    assert report["reviewer_expected_matches"]["false_accept"] == 1
    assert report["reviewer_expected_matches"]["reference_gate_pass"] is False
    assert report["human_acceptance"] == "pending"


def test_summary_does_not_drop_invalid_review_from_mixed_calibration(tmp_path):
    (tmp_path / "ledger.json").write_text(json.dumps({
        "max_calls": 2, "max_cost_usd": "1", "reserved_calls": 2,
        "reserved_cost_usd": "0.30", "used_calls": 2, "used_cost_usd": "0.02",
        "calls": [{"status": "completed"}, {"status": "completed"}],
    }))
    (tmp_path / "inputs.json").write_text(json.dumps({
        "fixtures": {
            "matrix": {"repeats": 1},
            "reviewer_fixtures": [{"id": "good"}, {"id": "malformed"}],
        }
    }))
    (tmp_path / "summary.json").write_text(json.dumps({
        "stage": "reviewer",
        "run_status": "complete",
        "results": [
            {
                "fixture_id": "good",
                "status": "reviewer_accept",
                "expected_label_pending_human_review": True,
                "matches_expected": True,
            },
            {
                "fixture_id": "malformed",
                "status": "local_parse_failure",
                "expected_label_pending_human_review": False,
                "matches_expected": False,
            },
        ],
    }))
    report = story_eval.summarize(tmp_path)["reviewer_expected_matches"]
    assert report["invalid_reviews"] == 1
    assert report["completed_run"] is True
    assert report["full_fixture_coverage"] is True
    assert report["reference_gate_pass"] is False
