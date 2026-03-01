"""Entity Resolver: deterministic normalization of LLM-extracted values to CRM IDs (RFC-004)."""

from app.core.entity_resolver.alias_resolver import AliasEntityResolver
from app.core.entity_resolver.branch_resolver import BranchResolver
from app.core.entity_resolver.models import ResolvedEntities, ResolvedEntity
from app.core.entity_resolver.protocol import EntityResolver
from app.core.entity_resolver.style_resolver import StyleResolver
from app.core.entity_resolver.teacher_resolver import TeacherResolver

__all__ = [
    "AliasEntityResolver",
    "BranchResolver",
    "EntityResolver",
    "ResolvedEntities",
    "ResolvedEntity",
    "StyleResolver",
    "TeacherResolver",
]
