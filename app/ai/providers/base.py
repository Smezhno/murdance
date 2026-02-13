"""LLM provider protocol interface.

Per CONTRACT §11: Provider selection and tool calling interface.
"""

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMResponse(BaseModel):
    """LLM response model."""

    text: str = Field(..., description="Response text")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, description="Tool calls if any")
    tokens_used: int = Field(..., description="Total tokens used")
    cost_usd: float = Field(default=0.0, description="Cost in USD")


class LLMProvider(Protocol):
    """Protocol for LLM providers (CONTRACT §11)."""

    async def call(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Call LLM with messages and optional tools.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions for function calling
            temperature: Temperature for generation (default 0.0 for deterministic)

        Returns:
            LLMResponse with text, tool_calls, tokens_used, cost_usd

        Raises:
            Exception: On API errors (will be caught by router)
        """
        ...

    async def health_check(self) -> bool:
        """Check if provider is available.

        Returns:
            True if provider is healthy, False otherwise
        """
        ...
