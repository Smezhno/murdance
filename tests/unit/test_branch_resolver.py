"""Unit tests for BranchResolver (RFC-004 §4.4, prompt 4.2.1)."""

from types import SimpleNamespace

import pytest

from app.core.entity_resolver.branch_resolver import BranchResolver


def _mock_branch(name: str, id_: str, crm_id: str | None, aliases: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=id_, crm_branch_id=crm_id or id_, aliases=aliases)


def _rfc_branches() -> list[SimpleNamespace]:
    """Branches from RFC-004 §4.4."""
    return [
        _mock_branch(
            "Гоголя",
            "gogolya",
            "XX",
            ["гоголя", "красного знамени", "красного знамени 59", "некрасовская", "первая речка"],
        ),
        _mock_branch(
            "Алеутская",
            "aleutskaya",
            "YY",
            ["алеутская", "центр", "родина", "клевер", "клевер хаус", "clover"],
        ),
        _mock_branch(
            "Семёновская",
            "semenovskaya",
            "ZZ",
            ["семёновская", "семеновская", "центр", "изумруд", "лотте"],
        ),
        _mock_branch(
            "Черемуховая",
            "cheremukhovaya",
            "WW",
            ["черемуховая", "чуркин", "чайка"],
        ),
    ]


def _mock_kb(
    branches: list[SimpleNamespace] | None = None,
    unknown_aliases: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        branches=branches or _rfc_branches(),
        unknown_areas={"aliases": unknown_aliases or ["вторая речка", "баляева", "заря", "седанка", "бам", "патрокл"]},
    )


@pytest.fixture
def resolver():
    return BranchResolver(_mock_kb())


def test_exact_name(resolver: BranchResolver) -> None:
    """resolve('гоголя') → [Гоголя]."""
    result = resolver.resolve("гоголя", "t")
    assert len(result) == 1
    assert result[0].name == "Гоголя"
    assert result[0].crm_id == "XX"


def test_alias(resolver: BranchResolver) -> None:
    """resolve('первая речка') → [Гоголя]."""
    result = resolver.resolve("первая речка", "t")
    assert len(result) == 1
    assert result[0].name == "Гоголя"


def test_ambiguous_center(resolver: BranchResolver) -> None:
    """resolve('центр') → [Алеутская, Семёновская] (len == 2)."""
    result = resolver.resolve("центр", "t")
    assert len(result) == 2
    names = {e.name for e in result}
    assert names == {"Алеутская", "Семёновская"}


def test_case_insensitive(resolver: BranchResolver) -> None:
    """resolve('ГОГОЛЯ') → [Гоголя]."""
    result = resolver.resolve("ГОГОЛЯ", "t")
    assert len(result) == 1
    assert result[0].name == "Гоголя"


def test_case_form(resolver: BranchResolver) -> None:
    """resolve('семёновской') → [Семёновская] (via pymorphy3 normal_form)."""
    result = resolver.resolve("семёновской", "t")
    assert len(result) == 1
    assert result[0].name == "Семёновская"


def test_unknown(resolver: BranchResolver) -> None:
    """resolve('что-то') → []."""
    result = resolver.resolve("что-то", "t")
    assert result == []


def test_unknown_area(resolver: BranchResolver) -> None:
    """check_unknown_area returns normalized (lowercase), e.g. 'Седанка' → 'седанка'."""
    assert resolver.check_unknown_area("седанка", "t") == "седанка"
    assert resolver.check_unknown_area("Седанка", "t") == "седанка"


def test_not_unknown_area(resolver: BranchResolver) -> None:
    """check_unknown_area('гоголя') → None (it's a branch, not unknown)."""
    assert resolver.check_unknown_area("гоголя", "t") is None


def test_branch_before_unknown() -> None:
    """If alias is in both branch and unknown_areas, branch wins: resolve() returns branch, check_unknown_area returns None."""
    kb = _mock_kb(
        branches=[
            _mock_branch("Тест", "test", "TT", ["конфликт"]),
        ],
        unknown_aliases=["конфликт"],
    )
    r = BranchResolver(kb)
    result = r.resolve("конфликт", "t")
    assert len(result) == 1
    assert result[0].name == "Тест"
    assert r.check_unknown_area("конфликт", "t") is None
