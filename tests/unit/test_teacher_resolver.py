"""Unit tests for TeacherResolver (RFC-004 §4.1, prompt 4.1.3)."""

import pytest
from pathlib import Path

from app.core.entity_resolver.teacher_resolver import TeacherResolver

# Path to names_dict.json next to the resolver module
NAMES_DICT_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "core" / "entity_resolver" / "names_dict.json"


class MockCRM:
    """CRM adapter that returns a fixed list of teachers."""

    def __init__(self, teachers: list[dict]) -> None:
        self.teachers = teachers

    async def get_teacher_list(self) -> list[dict]:
        return self.teachers


@pytest.fixture
def default_resolver():
    """Fresh resolver (not synced); each test calls sync(mock) as needed."""
    return TeacherResolver(NAMES_DICT_PATH)


@pytest.mark.asyncio
async def test_not_synced(default_resolver: TeacherResolver) -> None:
    """Before sync: resolve returns [], is_synced is False."""
    assert default_resolver.resolve("настя", "tenant") == []
    assert default_resolver.is_synced is False


@pytest.mark.asyncio
async def test_diminutive(default_resolver: TeacherResolver) -> None:
    """resolve('настя') → [ResolvedEntity(name='Анастасия Николаева', crm_id=1)]."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("настя", "tenant")
    assert len(result) == 1
    assert result[0].name == "Анастасия Николаева"
    assert result[0].crm_id == 1
    assert result[0].entity_type == "teacher"


@pytest.mark.asyncio
async def test_case_forms(default_resolver: TeacherResolver) -> None:
    """Падежи: насте, настю, настей, настюше → все резолвятся в crm_id=1."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    for form in ["насте", "настю", "настей", "настюше"]:
        result = default_resolver.resolve(form, "tenant")
        assert len(result) >= 1, f"form {form!r} should resolve"
        assert all(e.crm_id == 1 for e in result), f"form {form!r} should be crm_id=1"


@pytest.mark.asyncio
async def test_full_name(default_resolver: TeacherResolver) -> None:
    """resolve('анастасия') → crm_id=1."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("анастасия", "tenant")
    assert len(result) == 1
    assert result[0].crm_id == 1


@pytest.mark.asyncio
async def test_surname(default_resolver: TeacherResolver) -> None:
    """resolve('николаева') → crm_id=1."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("николаева", "tenant")
    assert len(result) == 1
    assert result[0].crm_id == 1


@pytest.mark.asyncio
async def test_surname_gender(default_resolver: TeacherResolver) -> None:
    """CRM has 'Анастасия Николаева'; resolve('николаев') → crm_id=1 (male form)."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("николаев", "tenant")
    assert len(result) == 1
    assert result[0].name == "Анастасия Николаева"
    assert result[0].crm_id == 1


@pytest.mark.asyncio
async def test_unknown(default_resolver: TeacherResolver) -> None:
    """resolve('вася') → []."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("вася", "tenant")
    assert result == []


@pytest.mark.asyncio
async def test_ambiguous(default_resolver: TeacherResolver) -> None:
    """Two Anastasias → resolve('настя') returns 2."""
    mock = MockCRM([
        {"id": 1, "name": "Анастасия Николаева"},
        {"id": 2, "name": "Анастасия Петрова"},
    ])
    await default_resolver.sync(mock)
    result = default_resolver.resolve("настя", "tenant")
    assert len(result) == 2
    ids = {e.crm_id for e in result}
    assert ids == {1, 2}


@pytest.mark.asyncio
async def test_case_insensitive(default_resolver: TeacherResolver) -> None:
    """resolve('НАСТЯ') same as resolve('настя')."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    r1 = default_resolver.resolve("НАСТЯ", "tenant")
    r2 = default_resolver.resolve("настя", "tenant")
    assert len(r1) == 1 and len(r2) == 1
    assert r1[0].crm_id == r2[0].crm_id == 1


@pytest.mark.asyncio
async def test_resync_updates(default_resolver: TeacherResolver) -> None:
    """Resync picks up new teacher; after second sync 'оля' resolves to Ольга Сидорова."""
    mock = MockCRM([{"id": 1, "name": "Анастасия Николаева"}])
    await default_resolver.sync(mock)
    assert default_resolver.resolve("оля", "t") == []

    mock.teachers.append({"id": 3, "name": "Ольга Сидорова"})
    await default_resolver.sync(mock)
    result = default_resolver.resolve("оля", "t")
    assert len(result) == 1
    assert result[0].crm_id == 3
    assert result[0].name == "Ольга Сидорова"
