"""공통 스키마 검증.

여기서 막지 못한 잘못된 이벤트는 규칙 단계까지 흘러가 조용히 틀린 결론이 된다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.core.schema import ACTIONS, AuditEvent, SchemaError, normalize_action


def make(**overrides) -> AuditEvent:
    base = dict(
        timestamp=datetime(2026, 1, 2, 23, 41, 7),
        actor="hong.gd",
        action="delete",
        target="Peak Table",
        equipment_id="sample",
        source_file="a.csv",
    )
    base.update(overrides)
    return AuditEvent(**base)


def test_creates_valid_event() -> None:
    event = make()
    assert event.action == "delete"
    assert event.hour == 23
    assert not event.has_reason


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_all_actions_are_accepted(action: str) -> None:
    assert make(action=action).action == action


def test_action_is_case_and_space_insensitive() -> None:
    assert normalize_action("  MODIFY ") == "modify"


def test_unknown_action_is_rejected_with_guidance() -> None:
    with pytest.raises(SchemaError) as info:
        make(action="Reviewed")
    assert "vocabulary" in str(info.value)


def test_timestamp_must_be_datetime() -> None:
    """문자열 시각을 통과시키면 시간 기반 규칙이 조용히 어긋난다."""
    with pytest.raises(SchemaError):
        make(timestamp="2026-01-02 23:41:07")


def test_actor_is_required() -> None:
    with pytest.raises(SchemaError):
        make(actor="   ")


@pytest.mark.parametrize("blank", ["", "  ", "-", "--", "N/A", "n/a", "nan", "NULL"])
def test_missing_value_markers_become_none(blank: str) -> None:
    """'값 없음'의 표기가 장비마다 달라도 규칙 쪽에서는 하나로 보여야 한다."""
    event = make(reason=blank, old_value=blank)
    assert event.reason is None
    assert event.old_value is None
    assert not event.has_reason


def test_raw_text_includes_column_names() -> None:
    """'Audit Trail' 이 값이 아니라 컬럼명 쪽에 있는 장비가 있다."""
    event = make(raw={"Audit Trail": "Off", "Item": "x"})
    assert "audit trail" in event.raw_text()
    assert "off" in event.raw_text()


def test_raw_text_hides_parser_metadata() -> None:
    event = make(raw={"Item": "x", "__line__": 42})
    assert "__line__" not in event.raw_text()
    assert "42" not in event.raw_text()


def test_raw_must_be_dict() -> None:
    with pytest.raises(SchemaError):
        make(raw=["not", "a", "dict"])


def test_raw_values_excludes_column_names() -> None:
    """낱말로 판단하는 규칙에서 컬럼명이 섞이면 오탐이 난다.

    'user_id' 라는 컬럼명 때문에 모든 행이 '계정 관련'으로 보인 적이 있다.
    """
    event = make(raw={"user_id": "op01", "message": "DOORS UNLOCK"})
    assert "user_id" not in event.raw_values()
    assert "op01" in event.raw_values()
    assert "doors unlock" in event.raw_values()
    assert "user_id" in event.raw_text()      # raw_text 는 컬럼명을 넣는다
