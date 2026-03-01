"""BranchResolver: aliases from KB → branch CRM IDs. Exact first, pymorphy3 fallback (RFC-004 §4.4)."""

from typing import Any, Protocol

from app.core.entity_resolver.inflect_helpers import get_morph, inflect_forms
from app.core.entity_resolver.models import ResolvedEntity


class _KbLike(Protocol):
    branches: list[Any]
    unknown_areas: Any  # optional: dict with "aliases": list[str]


def _add(lookup: dict[str, list[ResolvedEntity]], key: str, entry: ResolvedEntity) -> None:
    """Append entry to lookup[key] without duplicates."""
    key = key.strip().lower()
    if not key:
        return
    if key not in lookup:
        lookup[key] = []
    if not any(e.name == entry.name and e.crm_id == entry.crm_id for e in lookup[key]):
        lookup[key].append(entry)


def _build_lookup(kb: _KbLike) -> tuple[dict[str, list[ResolvedEntity]], set[str]]:
    """Build alias → list[ResolvedEntity]. One alias can map to multiple branches (e.g. центр)."""
    lookup: dict[str, list[ResolvedEntity]] = {}
    for branch in kb.branches:
        name = getattr(branch, "name", "").strip()
        crm_id = getattr(branch, "crm_branch_id", None) or getattr(branch, "id", "")
        if not name:
            continue
        aliases = getattr(branch, "aliases", None)
        if not aliases:
            aliases = [name.lower(), str(getattr(branch, "id", "")).lower()]
        entry = ResolvedEntity(
            name=name,
            crm_id=crm_id,
            entity_type="branch",
            confidence=1.0,
            source="alias",
        )
        for a in aliases:
            key = a.strip().lower()
            if key:
                _add(lookup, key, entry)
                for form in inflect_forms(key):
                    _add(lookup, form, entry)

    unknown: set[str] = set()
    ua = getattr(kb, "unknown_areas", None)
    if isinstance(ua, dict) and "aliases" in ua:
        for a in ua["aliases"] or []:
            if isinstance(a, str) and a.strip():
                key = a.strip().lower()
                unknown.add(key)
                for form in inflect_forms(key):
                    unknown.add(form)
    return lookup, unknown


class BranchResolver:
    """Resolves colloquial area/branch names to branch CRM IDs. Branch aliases before unknown_areas."""

    def __init__(self, kb: _KbLike) -> None:
        self._kb = kb
        self._lookup, self._unknown_aliases = _build_lookup(kb)

    def _lemmatize(self, text: str) -> str | None:
        words = text.split()
        if not words:
            return None
        try:
            morph = get_morph()
            lemmas = [morph.parse(w)[0].normal_form for w in words if morph.parse(w)]
            return " ".join(lemmas) if lemmas else None
        except Exception:
            return None

    def resolve(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
        """Exact match first, then pymorphy3 normal_form. Return [] if not found."""
        _ = tenant_id
        normalized = raw.strip().lower()
        if not normalized:
            return []
        if normalized in self._lookup:
            return list(self._lookup[normalized])
        lemma = self._lemmatize(normalized)
        if lemma and lemma in self._lookup:
            return list(self._lookup[lemma])
        return []

    def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
        """If raw matches unknown_areas alias (and is not a branch), return normalized name; else None."""
        _ = tenant_id
        normalized = raw.strip().lower()
        if not normalized:
            return None
        if normalized in self._lookup:
            return None
        lemma = self._lemmatize(normalized)
        if lemma and lemma in self._lookup:
            return None
        if normalized in self._unknown_aliases:
            return normalized
        if lemma and lemma in self._unknown_aliases:
            return normalized
        return None
