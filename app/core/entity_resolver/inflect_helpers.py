"""Shared pymorphy3 inflection for entity resolvers. Build-time only."""

import pymorphy3

_morph = pymorphy3.MorphAnalyzer()

_CASES = ("nomn", "gent", "datv", "accs", "ablt", "loct")


def inflect_forms(word: str) -> set[str]:
    """Generate Russian case forms for a single word.

    Returns set including the original word and all inflected forms.
    For multi-word strings — returns only the original (don't inflect phrases).
    """
    word = word.strip().lower()
    if not word or " " in word:
        return {word} if word else set()
    forms = {word}
    parsed = _morph.parse(word)
    if not parsed:
        return forms
    p = parsed[0]
    for case in _CASES:
        inflected = p.inflect({case})
        if inflected and inflected.word:
            forms.add(inflected.word)
    return forms


def get_morph() -> pymorphy3.MorphAnalyzer:
    """Return shared MorphAnalyzer instance."""
    return _morph
