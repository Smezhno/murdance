"""Unit tests for RFC-003 §8.1 KB extensions: BookingBranch, accessors, validators."""

import pytest

from app.knowledge.base import BookingBranch, KnowledgeBase


# ---------------------------------------------------------------------------
# Minimal valid KnowledgeBase fixture (no branches/dress_code/style_recs)
# ---------------------------------------------------------------------------

_MINIMAL_KB_DATA = {
    "schema_version": "1.0",
    "studio": {
        "name": "She Dance",
        "schedule": "Расписание через CRM",
        "timezone": "Asia/Vladivostok",
        "booking_branch": "Тест",
        "branches": [
            {"name": "Тест", "address": "Тест, 1", "phone": "+7 000 000-00-00", "halls": []}
        ],
    },
    "tone": {"style": "friendly", "pronouns": "ты"},
    "services": [
        {
            "id": "high-heels",
            "name": "High Heels",
            "description": "Танцы на каблуках",
            "price_single": 900,
            "aliases": ["хай хилс", "high heels"],
        },
        {
            "id": "girly-hiphop",
            "name": "Girly Hip-Hop",
            "description": "Женственный хип-хоп",
            "price_single": 900,
            "aliases": ["герли", "girly hip hop", "girly hiphop"],
        },
        {
            "id": "frame-up-strip",
            "name": "Frame Up Strip",
            "description": "Пластика и партер",
            "price_single": 900,
            "aliases": ["фрейм", "frame up", "frameup"],
        },
    ],
    "teachers": [
        {
            "id": "katya",
            "name": "Катя",
            "styles": ["High Heels"],
            "specialization": "Преподаватель",
            "aliases": [],
        }
    ],
    "escalation": {"triggers": ["жалоба"]},
}

_BRANCHES_DATA = [
    {
        "id": "semenovskaya",
        "name": "Семёновская",
        "crm_branch_id": "ZZ",
        "address": "Семёновская 30а (стеклянное здание, крайняя дверь справа)",
        "styles": ["high-heels", "frame-up-strip", "girly-hiphop", "vogue"],
        "aliases": ["семёновская", "семеновская", "центр"],
    },
    {
        "id": "gogolya",
        "name": "Гоголя",
        "crm_branch_id": "XX",
        "address": "Красного Знамени 59, 8 этаж (после лифта направо)",
        "styles": ["high-heels", "frame-up-strip", "dancehall"],
        "aliases": ["гоголя", "красного знамени", "первая речка"],
    },
    {
        "id": "cheremukhovaya",
        "name": "Черемуховая",
        "crm_branch_id": "WW",
        "address": "Черемуховая 40",
        "styles": ["frame-up-strip", "girly-hiphop"],
        "aliases": ["черемуховая", "чуркин", "чайка"],
    },
    {
        "id": "aleutskaya",
        "name": "Алеутская",
        "crm_branch_id": "YY",
        "address": "Алеутская 28",
        "styles": ["high-heels", "vogue", "dancehall"],
        "aliases": ["алеутская", "центр"],
    },
]

_DRESS_CODE_DATA = {
    "high-heels": "Любая удобная форма, сменная обувь — носочки либо каблуки со светлой подошвой",
    "frame-up-strip": "Любая удобная форма, сменная обувь — носочки либо каблуки со светлой подошвой",
    "girly-hiphop": "Любая удобная форма, сменная обувь — кроссовки",
    "dancehall": "Любая удобная форма, сменная обувь — кроссовки",
    "dancehall-female": "Любая удобная форма, сменная обувь — кроссовки",
    "vogue": "Любая удобная форма, сменная обувь — кроссовки либо каблуки",
}

_STYLE_RECS_DATA = {
    "feminine_heels": ["high-heels", "frame-up-strip"],
    "feminine_sneakers": ["girly-hiphop"],
    "energetic": ["dancehall", "dancehall-female"],
}


def _make_kb(**overrides) -> KnowledgeBase:
    data = {**_MINIMAL_KB_DATA, **overrides}
    return KnowledgeBase(**data)


def _full_kb() -> KnowledgeBase:
    return _make_kb(
        branches=_BRANCHES_DATA,
        dress_code=_DRESS_CODE_DATA,
        style_recommendations=_STYLE_RECS_DATA,
        promotions=[],
    )


# ---------------------------------------------------------------------------
# Backward compatibility: sections absent → defaults, no validation error
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_missing_branches_passes(self):
        kb = _make_kb()
        assert kb.branches == []

    def test_missing_dress_code_passes(self):
        kb = _make_kb()
        assert kb.dress_code == {}

    def test_missing_style_recommendations_passes(self):
        kb = _make_kb()
        assert kb.style_recommendations == {}

    def test_missing_promotions_passes(self):
        kb = _make_kb()
        assert kb.promotions == []


# ---------------------------------------------------------------------------
# BookingBranch model validation
# ---------------------------------------------------------------------------

class TestBookingBranchModel:
    def test_valid_branch(self):
        b = BookingBranch(
            id="test",
            name="Тест",
            crm_branch_id="TT",
            address="ул. Тест, 1",
            styles=["high-heels"],
            aliases=["тест"],
        )
        assert b.id == "test"

    def test_empty_styles_raises(self):
        with pytest.raises(Exception):
            BookingBranch(
                id="test",
                name="Тест",
                crm_branch_id="TT",
                address="ул. Тест, 1",
                styles=[],
                aliases=["тест"],
            )


# ---------------------------------------------------------------------------
# Validator: dress_code non-empty value check
# ---------------------------------------------------------------------------

class TestDressCodeValidator:
    def test_empty_value_raises(self):
        with pytest.raises(Exception):
            _make_kb(dress_code={"high-heels": ""})

    def test_whitespace_value_raises(self):
        with pytest.raises(Exception):
            _make_kb(dress_code={"high-heels": "   "})

    def test_valid_dress_code_passes(self):
        kb = _make_kb(dress_code={"high-heels": "Носочки или каблуки"})
        assert kb.dress_code["high-heels"] == "Носочки или каблуки"


# ---------------------------------------------------------------------------
# Validator: style_recommendations non-empty list check
# ---------------------------------------------------------------------------

class TestStyleRecommendationsValidator:
    def test_empty_category_list_raises(self):
        with pytest.raises(Exception):
            _make_kb(style_recommendations={"feminine_heels": []})

    def test_valid_recommendations_pass(self):
        kb = _make_kb(style_recommendations={"feminine_heels": ["high-heels"]})
        assert kb.style_recommendations["feminine_heels"] == ["high-heels"]


# ---------------------------------------------------------------------------
# get_branch()
# ---------------------------------------------------------------------------

class TestGetBranch:
    def setup_method(self):
        self.kb = _full_kb()

    def test_exact_id_match(self):
        branch = self.kb.get_branch("semenovskaya")
        assert branch is not None
        assert branch.id == "semenovskaya"

    def test_exact_name_match_case_insensitive(self):
        branch = self.kb.get_branch("семёновская")
        assert branch is not None
        assert branch.id == "semenovskaya"

    def test_exact_name_match_uppercase(self):
        branch = self.kb.get_branch("ГОГОЛЯ")
        assert branch is not None
        assert branch.id == "gogolya"

    def test_unknown_returns_none(self):
        assert self.kb.get_branch("Неизвестный") is None

    def test_partial_name_does_not_match(self):
        # No partial matching per plan
        assert self.kb.get_branch("Семён") is None

    def test_id_priority_over_name(self):
        # id match should return before name match
        branch = self.kb.get_branch("cheremukhovaya")
        assert branch.name == "Черемуховая"


# ---------------------------------------------------------------------------
# get_dress_code()
# ---------------------------------------------------------------------------

class TestGetDressCode:
    def setup_method(self):
        self.kb = _full_kb()

    def test_hyphen_key(self):
        result = self.kb.get_dress_code("high-heels")
        assert result is not None
        assert "каблуки" in result

    def test_underscore_key_normalized(self):
        result = self.kb.get_dress_code("high_heels")
        assert result is not None
        assert "каблуки" in result

    def test_hyphen_and_underscore_same_result(self):
        assert self.kb.get_dress_code("high-heels") == self.kb.get_dress_code("high_heels")

    def test_unknown_style_returns_none(self):
        assert self.kb.get_dress_code("unknown-style") is None

    def test_dancehall(self):
        result = self.kb.get_dress_code("dancehall")
        assert result is not None
        assert "кроссовки" in result

    def test_vogue(self):
        result = self.kb.get_dress_code("vogue")
        assert result is not None

    # --- CRM display name resolution (Fix 1) ---

    def test_crm_display_name_high_heels(self):
        """CRM returns 'High Heels' — must resolve via service aliases."""
        result = self.kb.get_dress_code("High Heels")
        assert result is not None
        assert result == self.kb.get_dress_code("high-heels")

    def test_crm_display_name_girly_hiphop(self):
        result = self.kb.get_dress_code("Girly Hip-Hop")
        assert result is not None
        assert result == self.kb.get_dress_code("girly-hiphop")

    def test_crm_display_name_frame_up_strip(self):
        result = self.kb.get_dress_code("Frame Up Strip")
        assert result is not None
        assert result == self.kb.get_dress_code("frame-up-strip")

    def test_unknown_crm_name_returns_none(self):
        assert self.kb.get_dress_code("неизвестный стиль") is None


# ---------------------------------------------------------------------------
# get_branch_address()
# ---------------------------------------------------------------------------

class TestGetBranchAddress:
    def setup_method(self):
        self.kb = _full_kb()

    def test_known_branch_by_name(self):
        addr = self.kb.get_branch_address("Семёновская")
        assert addr is not None
        assert "30а" in addr

    def test_known_branch_by_id(self):
        addr = self.kb.get_branch_address("aleutskaya")
        assert addr is not None
        assert "Алеутская" in addr

    def test_unknown_branch_returns_none(self):
        assert self.kb.get_branch_address("Несуществующий") is None


# ---------------------------------------------------------------------------
# get_active_promotions()
# ---------------------------------------------------------------------------

class TestGetActivePromotions:
    def test_empty_promotions(self):
        kb = _full_kb()
        assert kb.get_active_promotions() == []

    def test_returns_list_copy(self):
        kb = _make_kb(promotions=[{"name": "Акция", "discount": 10}])
        result = kb.get_active_promotions()
        assert result == [{"name": "Акция", "discount": 10}]
        result.clear()
        assert kb.promotions == [{"name": "Акция", "discount": 10}]


# ---------------------------------------------------------------------------
# format_for_llm() — no aliases in output (Fix 2)
# ---------------------------------------------------------------------------

class TestFormatForLlm:
    def setup_method(self):
        self.kb = _full_kb()

    def test_no_aliases_in_output(self):
        output = self.kb.format_for_llm()
        assert "хай хилс" not in output
        assert "high heals" not in output
        assert "Также:" not in output

    def test_service_names_present(self):
        output = self.kb.format_for_llm()
        assert "High Heels" in output

    def test_service_ids_present(self):
        output = self.kb.format_for_llm()
        assert "high-heels" in output
