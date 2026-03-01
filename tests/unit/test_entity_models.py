"""Unit tests for Entity Resolver models and SlotValues RFC-004 fields (prompt 4.1.1)."""

import pytest

from app.core.entity_resolver import ResolvedEntities, ResolvedEntity
from app.models import SlotValues


class TestResolvedEntity:
    """ResolvedEntity creation and fields."""

    def test_creation_and_fields(self) -> None:
        e = ResolvedEntity(
            name="Анастасия Николаева",
            crm_id=123,
            entity_type="teacher",
            confidence=1.0,
            source="names_dict",
        )
        assert e.name == "Анастасия Николаева"
        assert e.crm_id == 123
        assert e.entity_type == "teacher"
        assert e.confidence == 1.0
        assert e.source == "names_dict"

    def test_crm_id_str(self) -> None:
        e = ResolvedEntity(
            name="Гоголя",
            crm_id="gogolya",
            entity_type="branch",
            confidence=1.0,
            source="alias",
        )
        assert e.crm_id == "gogolya"


class TestResolvedEntities:
    """ResolvedEntities with empty lists."""

    def test_empty_lists(self) -> None:
        r = ResolvedEntities(teachers=[], branches=[], styles=[])
        assert r.teachers == []
        assert r.branches == []
        assert r.styles == []
        assert r.unknown_area is None

    def test_with_unknown_area(self) -> None:
        r = ResolvedEntities(
            teachers=[],
            branches=[],
            styles=[],
            unknown_area="седанка",
        )
        assert r.unknown_area == "седанка"


class TestSlotValuesBackwardCompat:
    """SlotValues(): no new fields works (existing PG sessions)."""

    def test_empty_slots_no_new_fields(self) -> None:
        slots = SlotValues()
        assert slots.teacher_raw is None
        assert slots.style_raw is None
        assert slots.branch_raw is None
        assert slots.teacher_id is None
        assert slots.branch_id is None
        assert slots.style_id is None

    def test_old_session_dict_deserializes(self) -> None:
        """Simulate JSONB from DB without RFC-004 keys — Pydantic accepts and defaults new fields."""
        old_dict = {
            "group": "High Heels",
            "teacher": "Анастасия",
            "branch": "Гоголя",
        }
        slots = SlotValues.model_validate(old_dict)
        assert slots.group == "High Heels"
        assert slots.teacher == "Анастасия"
        assert slots.branch == "Гоголя"
        assert slots.teacher_raw is None
        assert slots.teacher_id is None

    def test_old_session_with_extra_keys_ignored(self) -> None:
        """Dict with unknown keys (legacy/deprecated fields in DB) must still deserialize.

        Pydantic v2 ignores extra keys by default. If SlotValues ever gets
        model_config strict=True, this test would fail — reminder to handle extra keys.
        """
        dict_with_unknowns = {
            "group": "High Heels",
            "teacher": "Анастасия",
            "branch": "Гоголя",
            "legacy_slot_v1": "removed_field",
            "deprecated_intent": "old_booking",
            "some_typo_key": 42,
        }
        slots = SlotValues.model_validate(dict_with_unknowns)
        assert slots.group == "High Heels"
        assert slots.teacher == "Анастасия"
        assert slots.branch == "Гоголя"
        assert not hasattr(slots, "legacy_slot_v1")
        assert not hasattr(slots, "deprecated_intent")
        assert not hasattr(slots, "some_typo_key")


class TestSlotValuesNewFieldsSerialization:
    """SlotValues with new fields serializes/deserializes from JSONB."""

    def test_roundtrip_with_new_fields(self) -> None:
        slots = SlotValues(
            teacher_raw="настя",
            style_raw="каблуки",
            branch_raw="гоголя",
            teacher_id=123,
            branch_id="gogolya",
            style_id=5,
        )
        dumped = slots.model_dump(mode="json")
        assert dumped["teacher_raw"] == "настя"
        assert dumped["style_raw"] == "каблуки"
        assert dumped["branch_raw"] == "гоголя"
        assert dumped["teacher_id"] == 123
        assert dumped["branch_id"] == "gogolya"
        assert dumped["style_id"] == 5

        restored = SlotValues.model_validate(dumped)
        assert restored.teacher_raw == slots.teacher_raw
        assert restored.teacher_id == slots.teacher_id
        assert restored.branch_id == slots.branch_id
        assert restored.style_id == slots.style_id
