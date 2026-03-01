"""Unit tests for RFC-004 KB aliases: branches, style_aliases, unknown_areas, validation."""

import pytest

from app.knowledge.base import BookingBranch, KnowledgeBase


def _minimal_studio():
    return {
        "name": "She Dance",
        "schedule": "Расписание",
        "timezone": "Asia/Vladivostok",
        "address": "Владивосток",
        "phone": "+7 000 000-00-00",
    }


def _valid_branches():
    return [
        {
            "id": "gogolya",
            "name": "Гоголя",
            "crm_branch_id": "XX",
            "address": "Красного Знамени 59",
            "styles": ["high-heels"],
            "aliases": ["гоголя", "первая речка"],
        },
        {
            "id": "test",
            "name": "Тест",
            "crm_branch_id": "TT",
            "address": "Адрес тест",
            "styles": ["high-heels"],
            "aliases": ["тест"],
        },
    ]


def _valid_style_aliases():
    return {
        "High Heels": {"crm_style_id": 5, "aliases": ["хилс", "каблуки"]},
    }


def _valid_unknown_areas():
    return {
        "aliases": ["седанка", "баляева"],
        "response_template": "Нет филиала. Ближайшие: {nearest_branches}",
        "nearest_branches": {"седанка": ["Гоголя"]},
    }


def _make_kb(**overrides):
    data = {
        "schema_version": "1.0",
        "studio": _minimal_studio(),
        "tone": {"style": "friendly", "pronouns": "ты"},
        "services": [
            {"id": "high-heels", "name": "High Heels", "description": "Каблуки", "price_single": 900, "aliases": []},
        ],
        "teachers": [
            {"id": "t1", "name": "Тест", "styles": ["high-heels"], "specialization": "Тест", "aliases": []},
        ],
        "escalation": {"triggers": ["жалоба"]},
        "branches": _valid_branches(),
        "style_aliases": _valid_style_aliases(),
        "unknown_areas": _valid_unknown_areas(),
        **overrides,
    }
    return KnowledgeBase(**data)


class TestValidationPasses:
    """Validation passes with correct data."""

    def test_loads_with_branches_style_aliases_unknown_areas(self):
        kb = _make_kb()
        assert len(kb.branches) == 2
        assert "High Heels" in kb.style_aliases
        assert kb.unknown_areas["aliases"] == ["седанка", "баляева"]


class TestValidationFails:
    """Validation fails when constraints are violated."""

    def test_branch_alias_in_unknown_areas_raises(self):
        with pytest.raises(ValueError, match="both branches and unknown_areas"):
            _make_kb(
                unknown_areas={"aliases": ["гоголя"]},  # "гоголя" is branch alias
            )

    def test_missing_crm_branch_id_raises(self):
        with pytest.raises(Exception, match="crm_branch_id|Field required"):
            KnowledgeBase(
                schema_version="1.0",
                studio=_minimal_studio(),
                tone={"style": "f", "pronouns": "ты"},
                services=[{"id": "s1", "name": "S", "description": "D", "price_single": 0, "aliases": []}],
                teachers=[{"id": "t1", "name": "T", "styles": ["s1"], "specialization": "T", "aliases": []}],
                escalation={"triggers": []},
                branches=[
                    {
                        "id": "b1",
                        "name": "Б",
                        "address": "А",
                        "styles": ["high-heels"],
                        "aliases": ["б"],
                    },
                ],
            )

    def test_uppercase_alias_raises(self):
        with pytest.raises(ValueError, match="lowercase"):
            _make_kb(
                branches=[
                    {
                        "id": "b1",
                        "name": "Б",
                        "crm_branch_id": "B1",
                        "address": "А",
                        "styles": ["high-heels"],
                        "aliases": ["Гоголя"],  # uppercase
                    },
                ],
            )

    def test_style_aliases_empty_aliases_raises(self):
        with pytest.raises(ValueError, match="non-empty aliases"):
            _make_kb(style_aliases={"High Heels": {"crm_style_id": 5, "aliases": []}})

    def test_style_aliases_missing_crm_style_id_and_crm_id_raises(self):
        """Entry with neither crm_style_id nor crm_id must fail validation."""
        with pytest.raises(ValueError, match="crm_style_id|crm_id"):
            _make_kb(style_aliases={"Some Style": {"aliases": ["some"]}})


class TestAccessors:
    """get_branch_aliases, get_style_aliases return correct mapping."""

    def test_get_branch_aliases(self):
        kb = _make_kb()
        aliases = kb.get_branch_aliases()
        assert "гоголя" in aliases
        assert len(aliases["гоголя"]) == 1
        assert aliases["гоголя"][0]["name"] == "Гоголя"
        assert aliases["гоголя"][0]["crm_branch_id"] == "XX"
        assert "первая речка" in aliases
        assert "тест" in aliases

    def test_get_style_aliases(self):
        kb = _make_kb()
        styles = kb.get_style_aliases()
        assert "High Heels" in styles
        assert styles["High Heels"]["crm_style_id"] == 5
        assert "хилс" in styles["High Heels"]["aliases"]
        assert "каблуки" in styles["High Heels"]["aliases"]

    def test_get_unknown_areas(self):
        kb = _make_kb()
        ua = kb.get_unknown_areas()
        assert ua["aliases"] == ["седанка", "баляева"]
        assert "nearest_branches" in ua
