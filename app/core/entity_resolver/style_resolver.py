"""StyleResolver: style_aliases from KB → CRM style IDs. Exact first, pymorphy3 fallback (RFC-004 §4.5)."""

from typing import Any, Protocol

from app.core.entity_resolver.inflect_helpers import get_morph, inflect_forms
from app.core.entity_resolver.models import ResolvedEntity


class _KbLike(Protocol):
    style_aliases: Any  # dict[str, dict] with crm_style_id, aliases


def _add(lookup: dict[str, list[ResolvedEntity]], key: str, entry: ResolvedEntity) -> None:
    """Append entry to lookup[key] without duplicates."""
    key = key.strip().lower()
    if not key:
        return
    if key not in lookup:
        lookup[key] = []
    if not any(e.name == entry.name and e.crm_id == entry.crm_id for e in lookup[key]):
        lookup[key].append(entry)


def _build_lookup(kb: _KbLike) -> dict[str, list[ResolvedEntity]]:
    """Build alias → list[ResolvedEntity] from style_aliases."""
    lookup: dict[str, list[ResolvedEntity]] = {}
    aliases = getattr(kb, "style_aliases", None) or {}
    if not isinstance(aliases, dict):
        return lookup
    for style_name, data in aliases.items():
        if not isinstance(data, dict):
            continue
        crm_id = data.get("crm_style_id") or data.get("crm_id")
        alias_list = data.get("aliases") or []
        if crm_id is None:
            continue
        entry = ResolvedEntity(
            name=str(style_name),
            crm_id=crm_id,
            entity_type="style",
            confidence=1.0,
            source="alias",
        )
        for a in alias_list:
            if isinstance(a, str):
                key = a.strip().lower()
                if not key:
                    continue
                _add(lookup, key, entry)
                for form in inflect_forms(key):
                    _add(lookup, form, entry)
    return lookup


class StyleResolver:
    """Resolves colloquial style names to CRM style IDs."""

    def __init__(self, kb: _KbLike) -> None:
        self._lookup = _build_lookup(kb)

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
