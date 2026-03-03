"""Unit tests for _matches_filter and ID extraction helpers (RFC-006 Phase 2)."""

import pytest

from app.core.schedule_flow import (
    _entry_branch_id,
    _entry_teacher_id,
    _entry_style_id,
    _matches_filter,
)
from app.integrations.impulse.models import Schedule


def _entry(style_id: int | None = None, branch_id: str | int | None = None, teacher_id: int | None = None) -> Schedule:
    """Build a Schedule-like entry with group.style.id, branch.id, group.teacher1.id."""
    group: dict = {}
    if style_id is not None:
        group["style"] = {"id": style_id, "name": "Test Style"}
    if teacher_id is not None:
        group["teacher1"] = {"id": teacher_id, "name": "Test Teacher"}
    if not group:
        group["style"] = {"id": 99, "name": "Default"}
    branch = {"id": branch_id, "name": "Branch"} if branch_id is not None else {"id": 1, "name": "Default"}
    return Schedule(id=1, day=1, group=group, branch=branch)


def test_entry_style_id_from_group() -> None:
    """Schedule has no style_id attr; ID comes from group.style.id."""
    entry = _entry(style_id=5)
    assert hasattr(entry, "style_id") is False
    assert _entry_style_id(entry) == 5


def test_entry_branch_id_from_branch_dict() -> None:
    """Branch ID from entry.branch.id."""
    entry = _entry(branch_id="B1")
    assert _entry_branch_id(entry) == "B1"


def test_entry_teacher_id_from_group_teacher1() -> None:
    """Teacher ID from entry.group.teacher1.id."""
    entry = _entry(teacher_id=10)
    assert _entry_teacher_id(entry) == 10


def test_matches_filter_style_id_match() -> None:
    """When style_id filter is set and entry has same group.style.id, match."""
    entry = _entry(style_id=5, branch_id=1, teacher_id=10)
    assert _matches_filter(
        entry, style_id=5, branch_id=None, teacher_id=None,
        group_name=None, branch_name=None, teacher_name=None,
    ) is True


def test_matches_filter_style_id_mismatch() -> None:
    """When style_id filter is set and entry has different style id, no match."""
    entry = _entry(style_id=5)
    assert _matches_filter(
        entry, style_id=99, branch_id=None, teacher_id=None,
        group_name=None, branch_name=None, teacher_name=None,
    ) is False


def test_matches_filter_style_id_set_but_entry_missing_style() -> None:
    """When style_id filter is set but entry has no group.style, exclude (no silent pass)."""
    entry = Schedule(id=1, day=1, group={}, branch={"id": 1, "name": "X"})
    assert _entry_style_id(entry) is None
    assert _matches_filter(
        entry, style_id=5, branch_id=None, teacher_id=None,
        group_name=None, branch_name=None, teacher_name=None,
    ) is False


def test_matches_filter_branch_id_set_but_entry_missing_branch() -> None:
    """When branch_id filter is set but entry has no branch.id, exclude."""
    entry = Schedule(id=1, day=1, group={"style": {"id": 1}}, branch=None)
    assert _entry_branch_id(entry) is None
    assert _matches_filter(
        entry, style_id=None, branch_id="B1", teacher_id=None,
        group_name=None, branch_name=None, teacher_name=None,
    ) is False


def test_matches_filter_teacher_id_match() -> None:
    """When teacher_id filter is set and entry has same group.teacher1.id, match."""
    entry = _entry(teacher_id=10)
    assert _matches_filter(
        entry, style_id=None, branch_id=None, teacher_id=10,
        group_name=None, branch_name=None, teacher_name=None,
    ) is True


def test_matches_filter_string_fallback_style_name() -> None:
    """When style_id not set, filter by group_name (style_name) substring."""
    entry = _entry(style_id=1)
    # Schedule.style_name returns group["style"]["name"]
    assert getattr(entry, "style_name", "") == "Test Style"
    assert _matches_filter(
        entry, style_id=None, branch_id=None, teacher_id=None,
        group_name="Test", branch_name=None, teacher_name=None,
    ) is True
    assert _matches_filter(
        entry, style_id=None, branch_id=None, teacher_id=None,
        group_name="Other", branch_name=None, teacher_name=None,
    ) is False


def test_matches_filter_all_ids_match() -> None:
    """When all three IDs are set and entry matches all, match."""
    entry = _entry(style_id=5, branch_id="B1", teacher_id=10)
    assert _matches_filter(
        entry, style_id=5, branch_id="B1", teacher_id=10,
        group_name=None, branch_name=None, teacher_name=None,
    ) is True


def test_matches_filter_one_id_mismatch_excludes() -> None:
    """When one of style/branch/teacher ID does not match, no match."""
    entry = _entry(style_id=5, branch_id="B1", teacher_id=10)
    assert _matches_filter(
        entry, style_id=5, branch_id="B1", teacher_id=99,
        group_name=None, branch_name=None, teacher_name=None,
    ) is False
