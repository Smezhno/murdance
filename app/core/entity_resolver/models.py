"""Entity resolver data models (RFC-004 §4.6)."""

from dataclasses import dataclass


@dataclass
class ResolvedEntity:
    """Single resolved entity (teacher, branch, or style)."""

    name: str
    crm_id: int | str
    entity_type: str  # "teacher" | "branch" | "style"
    confidence: float  # 1.0 for exact match (fuzzy OFF in MVP)
    source: str  # "alias" | "names_dict" | "direct"


@dataclass
class ResolvedEntities:
    """Aggregate of resolved entities (optional container)."""

    teachers: list[ResolvedEntity]
    branches: list[ResolvedEntity]
    styles: list[ResolvedEntity]
    unknown_area: str | None = None
