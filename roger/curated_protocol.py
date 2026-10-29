"""Fixed Curated profiles and the provider protocol shared with offline evaluations."""

from __future__ import annotations

import dataclasses
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

PROFILE_HEADERS = {"X-OpenRouter-Metadata": "enabled", "X-OpenRouter-Cache": "false"}
MAX_INPUT_TOKENS = 32_768


@dataclasses.dataclass(frozen=True)
class CuratedProfile:
    name: str
    model: str
    returned_models: tuple[str, ...]
    max_tokens: int
    timeout: float
    prompt_usd_per_million: Decimal
    completion_usd_per_million: Decimal
    prompt_cache_write_usd_per_million: Decimal
    provider: str = "openai"
    provider_display_name: str = "OpenAI"
    service_tier: str = "default"
    reasoning_effort: str = "medium"
    prompt_cache: str = "disabled"


CURATED_WRITER = CuratedProfile(
    "sol", "openai/gpt-6.1-sol-20260929",
    ("openai/gpt-6.1-sol-20260929", "openai/gpt-6.1-sol"),
    16_384, 1200.0, Decimal("2"), Decimal("10"), Decimal("2.5"),
)
CURATED_REVIEWER = CuratedProfile(
    "astra", "openai/gpt-6-astra-20260903",
    ("openai/gpt-6-astra-20260903", "openai/gpt-6-astra"),
    8_192, 900.0, Decimal("10"), Decimal("50"), Decimal("12.5"),
)


def input_token_upper_bound(messages: list[dict[str, Any]], response_format: dict[str, Any]) -> int:
    content = json.dumps(
        {"messages": messages, "response_format": response_format},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return len(content) + 2_048


def request_kwargs(
    profile: Any, messages: list[dict[str, Any]], response_format: dict[str, Any],
    *, max_tokens: int, timeout: float,
) -> dict[str, Any]:
    if any(
        not isinstance(message, dict) or set(message) != {"role", "content"}
        or not isinstance(message["content"], str)
        for message in messages
    ):
        raise ValueError("profile messages must be plain text without cache controls")
    kwargs = {
        "model": profile.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "service_tier": profile.service_tier,
        "timeout": timeout,
        "extra_body": {
            "provider": {
                "only": [profile.provider],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {
                    "prompt": float(profile.prompt_usd_per_million),
                    "completion": float(profile.completion_usd_per_million),
                },
            },
            "reasoning": {"effort": profile.reasoning_effort, "exclude": True},
        },
    }
    if profile.prompt_cache == "disabled":
        kwargs["extra_body"]["prompt_cache_options"] = {"mode": "explicit"}
    return kwargs


def _is_passed_moderation(stage: Any) -> bool:
    if not isinstance(stage, dict) or set(stage) != {"type", "name", "summary", "data"}:
        return False
    data = stage["data"]
    return (
        stage["type"] == "guardrail"
        and stage["name"] == "moderation"
        and stage["summary"] == "Content passed OpenAI moderation"
        and isinstance(data, dict)
        and set(data) == {"engine", "flagged", "moderator_ids"}
        and data["engine"] == "openai-moderations"
        and data["flagged"] is False
        and data["moderator_ids"] == ["OpenAIOmniModerator"]
    )


def _pipeline_annotation(stage: Any) -> dict[str, Any]:
    if not isinstance(stage, dict):
        return {"type": str(stage), "recognized_pass_only": False}
    annotation = {
        key: stage[key]
        for key in ("type", "name", "summary")
        if isinstance(stage.get(key), str)
    }
    data = stage.get("data")
    if isinstance(data, dict):
        clean_data = {}
        if isinstance(data.get("engine"), str):
            clean_data["engine"] = data["engine"]
        if isinstance(data.get("flagged"), bool):
            clean_data["flagged"] = data["flagged"]
        moderator_ids = data.get("moderator_ids")
        if isinstance(moderator_ids, list) and all(
            isinstance(value, str) for value in moderator_ids
        ):
            clean_data["moderator_ids"] = moderator_ids
        if clean_data:
            annotation["data"] = clean_data
    annotation["recognized_pass_only"] = _is_passed_moderation(stage)
    return annotation


def _route_metadata(response: Any) -> dict[str, Any] | None:
    raw = getattr(response, "openrouter_metadata", None)
    if raw is None:
        extra = getattr(response, "model_extra", None)
        raw = extra.get("openrouter_metadata") if isinstance(extra, dict) else None
    if not isinstance(raw, dict):
        return None
    endpoints = raw.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    clean_endpoints = []
    if isinstance(available, list):
        for endpoint in available:
            if isinstance(endpoint, dict):
                clean_endpoints.append({
                    key: endpoint.get(key) for key in ("provider", "model", "selected")
                })
    pipeline = raw.get("pipeline")
    if isinstance(pipeline, list):
        pipeline_stages = [_pipeline_annotation(stage) for stage in pipeline]
        pipeline_pass_only = all(_is_passed_moderation(stage) for stage in pipeline)
    elif pipeline is None:
        pipeline_stages = []
        pipeline_pass_only = True
    else:
        pipeline_stages = [{"type": "invalid", "recognized_pass_only": False}]
        pipeline_pass_only = False
    attempts = raw.get("attempts")
    return {
        "requested": raw.get("requested"),
        "strategy": raw.get("strategy"),
        "attempt": raw.get("attempt"),
        "attempt_count": len(attempts) if isinstance(attempts, list) else None,
        "available": clean_endpoints,
        "pipeline_stages": pipeline_stages,
        "pipeline_pass_only": pipeline_pass_only,
    }


def _visible_response(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None)
    shape_error = None
    if choices is None:
        choice = None
        shape_error = "null_choices"
    elif not isinstance(choices, list) or not choices:
        choice = None
        shape_error = "empty_choices" if choices == [] else "invalid_choices"
    else:
        choice = choices[0]
        if choice is None:
            shape_error = "null_choice"
        elif len(choices) != 1:
            shape_error = "multiple_choices"
    message = getattr(choice, "message", None) if choice is not None else None
    if choice is not None and message is None:
        shape_error = "missing_message"
    visible = {
        "generation_id": _generation_id(response),
        "usage_details": _usage_details(response),
        "model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None),
        "content": getattr(message, "content", None),
        "refusal": getattr(message, "refusal", None),
        "service_tier": getattr(response, "service_tier", None),
        "route_metadata": _route_metadata(response),
    }
    if shape_error is not None:
        visible["shape_error"] = shape_error
    return visible


def _generation_id(response: Any) -> str | None:
    value = getattr(response, "id", None)
    if isinstance(value, str) and re.fullmatch(r"gen-[A-Za-z0-9_-]{1,252}", value):
        return value
    return None


def _usage_details(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cost_details = getattr(usage, "cost_details", None)

    def field(container: Any, key: str) -> Any:
        return container.get(key) if isinstance(container, dict) else getattr(container, key, None)

    counts = {}
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    for key in ("cached_tokens", "cache_write_tokens"):
        value = field(prompt_details, key)
        counts[key] = value if (
            type(value) is int and value >= 0
            and (type(prompt_tokens) is not int or prompt_tokens < 0 or value <= prompt_tokens)
        ) else None
    costs = {}
    for key in (
        "upstream_inference_cost", "upstream_inference_prompt_cost",
        "upstream_inference_completions_cost",
    ):
        value = field(cost_details, key)
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            amount = None
        costs[key] = (
            str(amount) if amount is not None and amount.is_finite() and amount >= 0 else None
        )
    return {**counts, "cost_details": costs}


def _usage(response: Any) -> dict[str, int | Decimal | None] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    cost = getattr(usage, "cost", None)
    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if cost is None or input_tokens is None or output_tokens is None:
        return None
    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None) if details else None
    try:
        parsed_cost = Decimal(str(cost))
    except (InvalidOperation, TypeError, ValueError):
        return None
    counts = (input_tokens, output_tokens)
    if (
        not parsed_cost.is_finite() or parsed_cost < 0
        or any(type(value) is not int or value < 0 for value in counts)
        or (reasoning_tokens is not None
            and (type(reasoning_tokens) is not int or reasoning_tokens < 0))
    ):
        return None
    return {
        "cost_usd": parsed_cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def _response_status(visible: dict[str, Any], profile: Any) -> str:
    if visible.get("shape_error") is not None:
        return "malformed_response"
    if visible["model"] not in profile.returned_models:
        return "model_mismatch"
    if visible["service_tier"] is not None and visible["service_tier"] != profile.service_tier:
        return "service_tier_mismatch"
    if visible["finish_reason"] == "length":
        return "finish_truncation"
    if visible["finish_reason"] == "content_filter":
        return "content_filter"
    if visible["refusal"]:
        return "refusal"
    if not isinstance(visible["content"], str) or not visible["content"].strip():
        return "blank_response"
    metadata = visible["route_metadata"]
    if metadata is None:
        return "missing_route_metadata"
    if metadata["requested"] != profile.model or metadata["attempt"] != 1:
        return "route_metadata_mismatch"
    if not metadata["pipeline_pass_only"]:
        return "pipeline_interference"
    if metadata["attempt_count"] not in (None, 1):
        return "routing_multiple_attempts"
    selected = [row for row in metadata["available"] if row["selected"] is True]
    if len(selected) != 1:
        return "missing_selected_endpoint"
    expected_provider = "".join(profile.provider_display_name.lower().split())
    actual_provider = "".join(str(selected[0]["provider"]).lower().split())
    if actual_provider != expected_provider or selected[0]["model"] not in profile.returned_models:
        return "route_mismatch"
    return "completed"


