"""YandexGPT Pro 5.1 provider implementation.

Primary LLM provider per economics decision.
"""

import time
from functools import lru_cache
from typing import Any

import httpx

from app.ai.providers.base import LLMProvider, LLMResponse
from app.config import get_settings


class YandexGPTProvider:
    """YandexGPT Pro 5.1 provider (CONTRACT §11)."""

    def __init__(self) -> None:
        """Initialize YandexGPT provider."""
        self.settings = get_settings()
        self.api_key = self.settings.yandexgpt_api_key
        self.folder_id = self.settings.yandexgpt_folder_id
        self.model = "yandexgpt-pro/latest"  # YandexGPT Pro 5.1
        self.base_url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
        # Persistent httpx client
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create persistent httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        """Close httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def call(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Call YandexGPT API.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions
            temperature: Temperature for generation

        Returns:
            LLMResponse with text, tool_calls, tokens_used, cost_usd

        Raises:
            httpx.HTTPError: On API errors
        """
        start_time = time.time()

        # Prepare request payload
        payload = {
            "modelUri": f"gpt://{self.folder_id}/{self.model}",
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": "2000",
            },
            "messages": [
                {"role": msg["role"], "text": msg["content"]} for msg in messages
            ],
        }

        # Add tools if provided
        if tools:
            payload["completionOptions"]["functionCall"] = {"type": "AUTO"}
            # Convert tools to YandexGPT format
            payload["functions"] = tools

        # Make API call with persistent client
        client = await self._get_client()
        response = await client.post(
            self.base_url,
            json=payload,
            headers={
                "Authorization": f"Api-Key {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

        # Parse response
        result = data.get("result", {})
        alternatives = result.get("alternatives", [])
        if not alternatives:
            raise ValueError("No alternatives in YandexGPT response")

        alternative = alternatives[0]
        text = alternative.get("message", {}).get("text", "")
        
        # Extract tool calls if any
        tool_calls: list[dict[str, Any]] = []
        if "functionCall" in alternative.get("message", {}):
            tool_calls.append(alternative["message"]["functionCall"])

        # Extract token usage
        usage = result.get("usage", {})
        tokens_used = usage.get("totalTokens", 0)

        # Calculate cost: YandexGPT Pro costs 0.41 RUB per 1000 tokens
        cost_per_1k_tokens_rub = 0.41
        cost_rub = (tokens_used / 1000.0) * cost_per_1k_tokens_rub
        # Convert to USD (1 USD ≈ 90 RUB)
        cost_usd = cost_rub / 90.0

        duration_ms = int((time.time() - start_time) * 1000)

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
        )

    async def health_check(self) -> bool:
        """Check if YandexGPT provider is available.

        Returns:
            True if provider is healthy, False otherwise
        """
        try:
            client = await self._get_client()
            response = await client.get(
                "https://llm.api.cloud.yandex.net",
                headers={"Authorization": f"Api-Key {self.api_key}"},
            )
            return response.status_code < 500
        except Exception:
            return False


_yandexgpt_provider: YandexGPTProvider | None = None


@lru_cache()
def get_yandexgpt_provider() -> YandexGPTProvider:
    """Get YandexGPT provider instance (lazy init)."""
    global _yandexgpt_provider
    if _yandexgpt_provider is None:
        _yandexgpt_provider = YandexGPTProvider()
    return _yandexgpt_provider
