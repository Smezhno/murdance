"""Impulse CRM HTTP client with retry and circuit breaker.

Per CONTRACT §5: HTTP Basic auth, retry with tenacity, circuit breaker.
"""

import base64
import time
from functools import lru_cache
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings


class CircuitBreaker:
    """Simple circuit breaker for CRM calls."""

    def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 60) -> None:
        """Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening
            timeout_seconds: Timeout before attempting to close
        """
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.is_open = False

    def record_success(self) -> None:
        """Record successful call."""
        self.failure_count = 0
        self.is_open = False
        self.last_failure_time = None

    def record_failure(self) -> bool:
        """Record failed call. Returns True if circuit should be open."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            return True

        return False

    def should_attempt(self) -> bool:
        """Check if call should be attempted."""
        if not self.is_open:
            return True

        # Check if timeout has passed
        if self.last_failure_time is None:
            return True

        if time.time() - self.last_failure_time > self.timeout_seconds:
            # Try to close circuit
            self.is_open = False
            self.failure_count = 0
            return True

        return False


class ImpulseClient:
    """Impulse CRM HTTP client (CONTRACT §5)."""

    def __init__(self) -> None:
        """Initialize Impulse client."""
        self.settings = get_settings()
        self.tenant = self.settings.crm_tenant
        self.api_key = self.settings.crm_api_key
        self.base_url = f"https://{self.tenant}.impulsecrm.ru/api"
        self.circuit_breaker = CircuitBreaker()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create httpx client."""
        if self._client is None:
            # Create Basic auth header
            auth_string = base64.b64encode(f"{self.api_key}:".encode()).decode()
            headers = {
                "Authorization": f"Basic {auth_string}",
                "Content-Type": "application/json",
            }
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close httpx client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def _request(
        self,
        method: str,
        entity: str,
        action: str,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Make HTTP request to Impulse CRM with retry.

        Args:
            method: HTTP method (GET, POST)
            entity: Entity name (schedule, client, etc.)
            action: Action name (list, load, update, delete)
            data: Request body data

        Returns:
            HTTP response

        Raises:
            httpx.HTTPError: On HTTP errors
        """
        if not self.circuit_breaker.should_attempt():
            raise RuntimeError("Circuit breaker is open")

        client = await self._get_client()
        url = f"/{entity}/{action}"

        try:
            if method == "GET":
                response = await client.get(url, params=data)
            else:
                response = await client.post(url, json=data)

            response.raise_for_status()
            self.circuit_breaker.record_success()
            return response

        except (httpx.HTTPError, httpx.TimeoutException) as e:
            self.circuit_breaker.record_failure()
            raise

    async def list(
        self,
        entity: str,
        fields: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
        page: int = 1,
        sort: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List entities (CONTRACT §5).

        Args:
            entity: Entity name
            fields: Fields to return
            filters: Filter columns
            limit: Page limit
            page: Page number
            sort: Sort order

        Returns:
            List of entity records
        """
        data: dict[str, Any] = {
            "limit": limit,
            "page": page,
        }
        if fields:
            data["fields"] = fields
        if filters:
            data["columns"] = filters
        if sort:
            data["sort"] = sort

        response = await self._request("POST", entity, "list", data)
        result = response.json()

        # Handle response format
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        if isinstance(result, list):
            return result
        return []

    async def load(self, entity: str, entity_id: int) -> dict[str, Any]:
        """Load single entity by ID (CONTRACT §5).

        Args:
            entity: Entity name
            entity_id: Entity ID

        Returns:
            Entity record
        """
        response = await self._request("GET", entity, "load", {"id": entity_id})
        return response.json()

    async def create(self, entity: str, data: dict[str, Any]) -> dict[str, Any]:
        """Create entity (CONTRACT §5).

        Args:
            entity: Entity name
            data: Entity data

        Returns:
            Created entity record
        """
        response = await self._request("POST", entity, "update", data)
        return response.json()

    async def update(self, entity: str, entity_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Update entity (CONTRACT §5).

        Args:
            entity: Entity name
            entity_id: Entity ID
            data: Update data

        Returns:
            Updated entity record
        """
        data["id"] = entity_id
        response = await self._request("POST", entity, "update", data)
        return response.json()

    async def delete(self, entity: str, entity_id: int) -> bool:
        """Delete entity (CONTRACT §5).

        Args:
            entity: Entity name
            entity_id: Entity ID

        Returns:
            True if deleted
        """
        response = await self._request("POST", entity, "delete", {"id": entity_id})
        return response.status_code == 200

    async def health_check(self) -> bool:
        """Check CRM health (CONTRACT §5).

        Returns:
            True if CRM is healthy
        """
        try:
            # Try to list groups (lightweight operation)
            await self.list("group", limit=1)
            return True
        except Exception:
            return False


@lru_cache()
def get_impulse_client() -> ImpulseClient:
    """Get Impulse client instance (lazy init)."""
    return ImpulseClient()

