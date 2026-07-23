"""OpenAI-compatible client pointed at OpenRouter, with budget checks and usage recording.

One client, one place. Failover across a brain's model chain is OpenRouter's job (the ``models``
array in ``extra_body``); this wrapper keeps exactly one retry for transport-level errors reaching
OpenRouter itself, per the spec.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from roger.config import Settings
from roger.store import Store

log = logging.getLogger("roger.llm")

# Sampling + output ceilings per brain (§11).
_TEMPERATURE = {"admin": 0.1, "digest": 0.3, "ambient": 0.8}
_MAX_TOKENS = {"admin": 1024, "digest": 1500, "ambient": 300}


class LLMConfigError(RuntimeError):
    """A brain was invoked with no models configured (MODEL_<BRAIN> is empty)."""


class BudgetExceeded(RuntimeError):
    def __init__(self, brain: str, used: int, cap: int) -> None:
        super().__init__(f"{brain} daily token budget exceeded ({used} >= {cap})")
        self.brain = brain
        self.used = used
        self.cap = cap


class LLM:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._store = store
        # max_retries=0: we do our own single, transport-only retry below.
        self._client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            max_retries=0,
        )
        self._chains = {
            "admin": settings.admin_models,
            "ambient": settings.ambient_models,
            "digest": settings.digest_models,
        }
        self._caps = {
            "admin": settings.daily_tokens_admin,
            "ambient": settings.daily_tokens_ambient,
            "digest": settings.daily_tokens_digest,
        }

    async def complete(
        self,
        brain: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        chain = self._chains[brain]
        if not chain:
            raise LLMConfigError(f"no models configured for {brain} (set MODEL_{brain.upper()})")

        used = await self._store.usage_today(brain)
        cap = self._caps[brain]
        if used >= cap:
            raise BudgetExceeded(brain, used, cap)

        extra_body: dict[str, Any] = {"models": chain}
        if tools:
            # Never route to a provider endpoint that silently lacks tool support.
            extra_body["provider"] = {"require_parameters": True}

        kwargs: dict[str, Any] = {
            "model": chain[0],
            "messages": messages,
            "temperature": _TEMPERATURE[brain],
            "max_tokens": _MAX_TOKENS[brain],
            "extra_body": extra_body,
        }
        if tools:
            kwargs["tools"] = tools

        response = await self._call_with_one_retry(kwargs)

        usage = getattr(response, "usage", None)
        if usage is not None:
            # `cost` is an OpenRouter extension on the usage object (USD, always returned now);
            # absent when pointed at a vanilla OpenAI-compatible host, so default to 0.
            await self._store.add_usage(
                brain,
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                cost_usd=float(getattr(usage, "cost", 0.0) or 0.0),
            )
        return response

    async def _call_with_one_retry(self, kwargs: dict[str, Any]) -> Any:
        for attempt in range(2):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except (APIConnectionError, APITimeoutError):
                if attempt == 1:
                    raise
                log.warning("transport error reaching OpenRouter; retrying once")
                await asyncio.sleep(0.5)
