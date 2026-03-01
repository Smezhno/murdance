"""EntityResolver protocol — interface for normalizing LLM-extracted values to CRM IDs (RFC-004 §4.6)."""

from typing import Protocol

from app.core.entity_resolver.models import ResolvedEntity


class EntityResolver(Protocol):
    """Each method receives a PRE-EXTRACTED value from LLM slot_updates, not raw user text."""

    async def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...
    async def resolve_branch(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...
    async def resolve_style(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...

    async def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
        """Return unknown area name if found, else None."""
        ...
