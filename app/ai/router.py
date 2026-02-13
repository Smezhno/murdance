"""LLM Router: provider selection and tool calling interface.

Per CONTRACT §11: Provider selection, tool calling, error handling.
"""

import time
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.ai.budget_guard import get_budget_guard
from app.ai.providers.base import LLMProvider, LLMResponse
from app.ai.providers.yandexgpt import get_yandexgpt_provider
from app.config import get_settings
from app.storage.postgres import postgres_storage


class LLMRouter:
    """LLM Router for provider selection and tool calling (CONTRACT §11)."""

    def __init__(self) -> None:
        """Initialize LLM router."""
        self.settings = get_settings()
        self.primary_provider: LLMProvider = get_yandexgpt_provider()
        self.fallback_provider: LLMProvider | None = None  # Will be set if anthropic key exists

    async def call(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        trace_id: UUID | None = None,
    ) -> LLMResponse:
        """Call LLM with provider selection and error handling.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions
            temperature: Temperature for generation
            trace_id: Trace ID for logging

        Returns:
            LLMResponse with text, tool_calls, tokens_used, cost_usd

        Raises:
            Exception: If all providers fail or budget exceeded
        """
        # Check budget limits before calling
        # Estimate tokens (rough: 1 token ≈ 4 chars)
        estimated_tokens = sum(len(msg.get("content", "")) for msg in messages) // 4
        # Estimate cost: YandexGPT Pro costs 0.41 RUB per 1000 tokens, convert to USD
        estimated_cost_rub = (estimated_tokens / 1000.0) * 0.41
        estimated_cost_usd = estimated_cost_rub / 90.0  # 1 USD ≈ 90 RUB

        budget_guard = get_budget_guard()
        within_limits, breach_reason = await budget_guard.check_all_limits(estimated_tokens, estimated_cost_usd)
        if not within_limits:
            # Log breach
            if trace_id:
                await postgres_storage.log_error(
                    error_type="BudgetBreach",
                    error_message=f"Budget limit exceeded: {breach_reason}",
                    trace_id=trace_id,
                )
            raise RuntimeError(f"Budget limit exceeded: {breach_reason}")

        start_time = time.time()

        try:
            # Try primary provider
            response = await self.primary_provider.call(messages, tools, temperature)

            # Note: check_all_limits already incremented counters, no need to increment again

            # Log LLM call
            if trace_id:
                duration_ms = int((time.time() - start_time) * 1000)
                await postgres_storage.log_llm_call(
                    trace_id=trace_id,
                    provider="yandexgpt",
                    model="yandexgpt-pro/latest",
                    prompt_tokens=estimated_tokens,  # Approximate
                    completion_tokens=response.tokens_used - estimated_tokens,
                    total_tokens=response.tokens_used,
                    cost_usd=response.cost_usd,
                    request_json={"messages": messages, "tools": tools},
                    response_json={"text": response.text, "tool_calls": response.tool_calls},
                    duration_ms=duration_ms,
                )

            return response

        except Exception as e:
            # Record error
            budget_guard = get_budget_guard()
            await budget_guard.record_error()

            # Log error
            if trace_id:
                duration_ms = int((time.time() - start_time) * 1000)
                await postgres_storage.log_llm_call(
                    trace_id=trace_id,
                    provider="yandexgpt",
                    model="yandexgpt-pro/latest",
                    error=str(e),
                    duration_ms=duration_ms,
                )

            # Try fallback provider if available
            if self.fallback_provider:
                try:
                    return await self.fallback_provider.call(messages, tools, temperature)
                except Exception:
                    pass  # Fallback also failed

            raise


_llm_router: LLMRouter | None = None


@lru_cache()
def get_llm_router() -> LLMRouter:
    """Get LLM router instance (lazy init)."""
    global _llm_router
    if _llm_router is None:
        _llm_router = LLMRouter()
    return _llm_router
