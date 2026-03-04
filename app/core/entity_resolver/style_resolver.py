"""StyleResolver: style_aliases from KB → CRM style IDs. Exact first, pymorphy3 fallback (RFC-004 §4.5)."""

import re
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
    """Build alias → list[ResolvedEntity] from style_aliases + services[].aliases merge."""
    lookup: dict[str, list[ResolvedEntity]] = {}
    aliases = getattr(kb, "style_aliases", None) or {}
    if not isinstance(aliases, dict):
        return lookup
    services = getattr(kb, "services", None) or []
    for style_name, data in aliases.items():
        if not isinstance(data, dict):
            continue
        crm_id = data.get("crm_style_id") or data.get("crm_id")
        if crm_id is None:
            continue
        alias_list = list(data.get("aliases") or [])
        for svc in services:
            if getattr(svc, "name", None) == style_name:
                alias_list.extend(getattr(svc, "aliases", []) or [])
                break
        entry = ResolvedEntity(
            name=str(style_name),
            crm_id=crm_id,
            entity_type="style",
            confidence=1.0,
            source="alias",
        )
        canonical_key = str(style_name).strip().lower()
        if canonical_key:
            _add(lookup, canonical_key, entry)
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
        """Exact match first, then lemma, then normalized, then prefix match. Split by «и»/comma (RFC-007 F10)."""
        _ = tenant_id
        normalized = raw.strip().lower()
        if not normalized:
            return []

        parts = re.split(r"\s+и\s+|,\s*", raw)
        if len(parts) > 1:
            seen: set[tuple[str, Any]] = set()
            all_results: list[ResolvedEntity] = []
            for part in parts:
                sub = part.strip()
                if not sub:
                    continue
                for e in self.resolve(sub, tenant_id):
                    key = (e.name, e.crm_id)
                    if key not in seen:
                        seen.add(key)
                        all_results.append(e)
            if all_results:
                return all_results

        if normalized in self._lookup:
            return list(self._lookup[normalized])
        lemma = self._lemmatize(normalized)
        if lemma and lemma in self._lookup:
            return list(self._lookup[lemma])

        normalized = raw.replace("-", " ").replace("  ", " ").strip().lower()
        if normalized in self._lookup:
            return list(self._lookup[normalized])

        if len(normalized) >= 3:
            matches: list[ResolvedEntity] = []
            for alias, entities in self._lookup.items():
                alias_norm = alias.replace("-", " ")
                if normalized in alias_norm or alias_norm.startswith(normalized):
                    for e in entities:
                        if not any(m.name == e.name and m.crm_id == e.crm_id for m in matches):
                            matches.append(e)
            if matches:
                return matches
        return []
