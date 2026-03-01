"""Integration tests for AliasEntityResolver (RFC-004 §4.6, prompt 4.2.4)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.entity_resolver import AliasEntityResolver, BranchResolver, StyleResolver, TeacherResolver

NAMES_DICT_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "core" / "entity_resolver" / "names_dict.json"


class MockCRM:
    def __init__(self, teachers: list[dict]) -> None:
        self.teachers = teachers

    async def get_teacher_list(self) -> list[dict]:
        return self.teachers


def _mock_kb() -> SimpleNamespace:
    return SimpleNamespace(
        branches=[
            SimpleNamespace(name="Гоголя", id="gogolya", crm_branch_id="XX", aliases=["гоголя"]),
        ],
        unknown_areas={"aliases": ["седанка"]},
        style_aliases={"High Heels": {"crm_style_id": 5, "aliases": ["каблуки", "хилс"]}},
    )


@pytest.fixture
def alias_resolver():
    teacher = TeacherResolver(NAMES_DICT_PATH)
    branch = BranchResolver(_mock_kb())
    style = StyleResolver(_mock_kb())
    return AliasEntityResolver(teacher, branch, style)


@pytest.mark.asyncio
async def test_resolve_teacher_delegates(alias_resolver: AliasEntityResolver) -> None:
    """resolve_teacher('настя') delegates to teacher_resolver; after sync returns result."""
    mock_crm = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await alias_resolver._teacher.sync(mock_crm)
    result = await alias_resolver.resolve_teacher("настя", "t1")
    assert len(result) == 1
    assert result[0].name == "Анастасия Николаева"
    assert result[0].crm_id == 1


@pytest.mark.asyncio
async def test_resolve_branch_delegates(alias_resolver: AliasEntityResolver) -> None:
    """resolve_branch('гоголя') delegates to branch_resolver."""
    result = await alias_resolver.resolve_branch("гоголя", "t1")
    assert len(result) == 1
    assert result[0].name == "Гоголя"
    assert result[0].crm_id == "XX"


@pytest.mark.asyncio
async def test_is_ready_false_before_sync(alias_resolver: AliasEntityResolver) -> None:
    assert alias_resolver.is_ready is False


@pytest.mark.asyncio
async def test_is_ready_true_after_sync(alias_resolver: AliasEntityResolver) -> None:
    mock_crm = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await alias_resolver._teacher.sync(mock_crm)
    assert alias_resolver.is_ready is True
