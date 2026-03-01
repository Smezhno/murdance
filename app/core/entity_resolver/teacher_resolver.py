"""TeacherResolver: names_dict + CRM sync, resolve via exact match then pymorphy3 (RFC-004 §4.1)."""

from pathlib import Path
from typing import Any

from app.core.entity_resolver.inflect_helpers import get_morph, inflect_forms
from app.core.entity_resolver.models import ResolvedEntity


def _load_names_dict(path: Path) -> dict[str, list[str]]:
    """Load names_dict.json: canonical → list of diminutives."""
    import json

    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k.lower(): [d.lower() for d in v] for k, v in data.items() if isinstance(v, list) and v}


def _build_inverted_index(canonical_to_diminutives: dict[str, list[str]]) -> dict[str, list[str]]:
    """Build alias → list[canonical]. One alias can map to multiple canonicals (e.g. саша → александр, александра)."""
    inverted: dict[str, list[str]] = {}
    for canonical, diminutives in canonical_to_diminutives.items():
        for dim in diminutives:
            dim = dim.lower()
            if dim not in inverted:
                inverted[dim] = []
            if canonical not in inverted[dim]:
                inverted[dim].append(canonical)
    return inverted


def _surname_opposite_gender(surname: str) -> str | None:
    """Return opposite gender form for Russian surname (string-only). Explicit -ова/-ева/-ина only."""
    s = surname.strip().lower()
    if not s:
        return None
    # Female → male: explicit -ова, -ева, -ина only (no generic -а fallback)
    if s.endswith("ова"):
        return s[:-2] + "ов"
    if s.endswith("ева"):
        return s[:-3] + "ев"  # -ева → -ев (3 chars)
    if s.endswith("ина"):
        return s[:-2] + "ин"
    # Male → female: -ов/-ев/-ин → add -а
    if s.endswith("ов") and not s.endswith("ова"):
        return s + "а"
    if s.endswith("ев") and not s.endswith("ева"):
        return s + "а"
    if s.endswith("ин") and not s.endswith("ина"):
        return s + "а"
    return None


class TeacherResolver:
    """Resolves diminutive/colloquial teacher names to CRM IDs. Exact match first, pymorphy3 fallback."""

    def __init__(self, names_dict_path: Path) -> None:
        self._names_dict_path = names_dict_path
        self._canonical_to_diminutives = _load_names_dict(names_dict_path)
        self._inverted = _build_inverted_index(self._canonical_to_diminutives)
        self._lookup: dict[str, list[ResolvedEntity]] = {}
        self._synced = False

    async def sync(self, crm_adapter: Any) -> None:
        """
        1. Call CRM: teacher/list → list of {id, name}
        2. For each teacher: split name, names_dict diminutives + first + full + surname (both genders)
        3. Store lookup: alias → list[ResolvedEntity]
        """
        teachers = await crm_adapter.get_teacher_list()
        lookup: dict[str, list[ResolvedEntity]] = {}

        for t in teachers:
            tid = t.get("id")
            name = t.get("name") or ""
            if tid is None or not name.strip():
                continue
            full = name.strip()
            parts = full.split()
            first = parts[0].lower() if parts else ""
            last = parts[-1].lower() if len(parts) > 1 else ""

            def add(key: str, full_name: str, crm_id: int | str) -> None:
                key = key.lower()
                entity = ResolvedEntity(
                    name=full_name,
                    crm_id=crm_id,
                    entity_type="teacher",
                    confidence=1.0,
                    source="names_dict" if key in self._inverted else "direct",
                )
                if key not in lookup:
                    lookup[key] = []
                if not any(e.crm_id == crm_id and e.name == full_name for e in lookup[key]):
                    lookup[key].append(entity)

            for form in inflect_forms(first):
                add(form, full, tid)
            for form in inflect_forms(full.lower()):
                add(form, full, tid)
            for alias in (last, _surname_opposite_gender(last)):
                if alias:
                    for form in inflect_forms(alias):
                        add(form, full, tid)
            for alias, canonicals in self._inverted.items():
                if first in canonicals:
                    for form in inflect_forms(alias):
                        add(form, full, tid)

        self._lookup = lookup
        self._synced = True

    def resolve(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
        """
        Algorithm: 1) normalized = raw.strip().lower(); 2) exact in _lookup → return;
        3) pymorphy3 normal_form → lookup; 4) else [].
        NO fuzzy. pymorphy3 only when exact fails.
        """
        _ = tenant_id
        normalized = raw.strip().lower()
        if not normalized:
            return []
        if normalized in self._lookup:
            return list(self._lookup[normalized])
        try:
            morph = get_morph()
            parsed = morph.parse(normalized)
            if parsed:
                lemma = parsed[0].normal_form
                if lemma in self._lookup:
                    return list(self._lookup[lemma])
        except Exception:
            pass
        return []

    @property
    def is_synced(self) -> bool:
        """True if sync completed successfully at least once."""
        return self._synced
