"""Unit tests for StyleResolver (RFC-004 §4.5, prompt 4.2.2)."""

from types import SimpleNamespace

import pytest

from app.core.entity_resolver.style_resolver import StyleResolver


def _mock_kb() -> SimpleNamespace:
    """KB with style_aliases from RFC-004 §4.5."""
    return SimpleNamespace(
        style_aliases={
            "High Heels": {"crm_style_id": 5, "aliases": ["хилс", "хиллз", "хилз", "каблуки", "heels", "на каблуках"]},
            "Girly Hip-Hop": {"crm_style_id": 8, "aliases": ["гёрли", "герли", "girly", "гёрл"]},
            "Contemporary": {"crm_style_id": 3, "aliases": ["контемп", "контемпорари", "contemporary"]},
        },
    )


@pytest.fixture
def resolver() -> StyleResolver:
    return StyleResolver(_mock_kb())


def test_alias(resolver: StyleResolver) -> None:
    """resolve('каблуки') → [High Heels]."""
    result = resolver.resolve("каблуки", "t")
    assert len(result) == 1
    assert result[0].name == "High Heels"
    assert result[0].crm_id == 5


def test_slang(resolver: StyleResolver) -> None:
    """resolve('гёрли') → [Girly Hip-Hop]."""
    result = resolver.resolve("гёрли", "t")
    assert len(result) == 1
    assert result[0].name == "Girly Hip-Hop"
    assert result[0].crm_id == 8


def test_case_form(resolver: StyleResolver) -> None:
    """resolve('каблуках') → [High Heels] (via pymorphy3)."""
    result = resolver.resolve("каблуках", "t")
    assert len(result) == 1
    assert result[0].name == "High Heels"


def test_english(resolver: StyleResolver) -> None:
    """resolve('heels') → [High Heels]."""
    result = resolver.resolve("heels", "t")
    assert len(result) == 1
    assert result[0].name == "High Heels"


def test_unknown(resolver: StyleResolver) -> None:
    """resolve('балет') → []."""
    result = resolver.resolve("балет", "t")
    assert result == []


def test_case(resolver: StyleResolver) -> None:
    """resolve('ХИЛС') → [High Heels]."""
    result = resolver.resolve("ХИЛС", "t")
    assert len(result) == 1
    assert result[0].name == "High Heels"
