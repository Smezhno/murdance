"""AliasEntityResolver: implements EntityResolver by delegating to teacher/branch/style resolvers (RFC-004 §4.6)."""

from app.core.entity_resolver.branch_resolver import BranchResolver
from app.core.entity_resolver.models import ResolvedEntity
from app.core.entity_resolver.style_resolver import StyleResolver
from app.core.entity_resolver.teacher_resolver import TeacherResolver


class AliasEntityResolver:
    """Implements EntityResolver Protocol. Combines three resolvers. Single-tenant."""

    def __init__(
        self,
        teacher_resolver: TeacherResolver,
        branch_resolver: BranchResolver,
        style_resolver: StyleResolver,
    ) -> None:
        self._teacher = teacher_resolver
        self._branch = branch_resolver
        self._style = style_resolver

    async def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
        return self._teacher.resolve(raw, tenant_id)

    async def resolve_branch(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
        return self._branch.resolve(raw, tenant_id)

    async def resolve_style(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
        return self._style.resolve(raw, tenant_id)

    async def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
        return self._branch.check_unknown_area(raw, tenant_id)

    @property
    def is_ready(self) -> bool:
        """True if teacher sync completed."""
        return self._teacher.is_synced
