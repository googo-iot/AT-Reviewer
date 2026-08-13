"""중복 판정과 정렬 검증.

여기서 실수하면 산출물에서 실제 이벤트가 조용히 사라진다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.core.export import deduplicate, overlap_summary, sort_events
from src.core.schema import AuditEvent


def event(
    when: str,
    *,
    action: str = "login",
    actor: str = "op01",
    source: str = "2025-06.pdf",
    raw: dict | None = None,
    target: str = "",
) -> AuditEvent:
    return AuditEvent(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        actor=actor,
        action=action,
        target=target,
        equipment_id="demo",
        source_file=str(Path("data") / source),
        raw=raw if raw is not None else {},
    )


# --------------------------------------------------------------------------
# 지우면 안 되는 것
# --------------------------------------------------------------------------


def test_distinct_events_in_same_second_are_not_merged() -> None:
    """정규화 때문에 서로 다른 사건이 같아 보이면 안 된다.

    실제 데이터에서 같은 초에 찍힌 'Successful login' 과 'Logout' 이
    둘 다 login 으로 정규화되어 하나가 지워질 뻔했다.
    """
    login = event("2025-03-06 15:17:32", raw={"Action": "Successful login"})
    logout = event("2025-03-06 15:17:32", raw={"Action": "Logout"})

    kept, removed = deduplicate([login, logout])

    assert len(kept) == 2
    assert not removed


def test_same_event_in_one_file_is_kept_twice_when_records_differ() -> None:
    same_time = "2025-06-30 08:01:19"
    queued = event(same_time, action="execute", raw={"Action": "Test queued"})
    started = event(same_time, action="execute", raw={"Action": "Test started"})

    kept, _ = deduplicate([queued, started])

    assert len(kept) == 2


# --------------------------------------------------------------------------
# 지워야 하는 것 + 어느 사본을 남길지
# --------------------------------------------------------------------------


def test_cross_file_duplicate_prefers_matching_filename() -> None:
    """내보내기 구간이 겹칠 때, 그 사건이 원래 속한 파일 것을 남긴다."""
    raw = {"Action": "Setting changed", "Date": "20.Jun.2025 09:43:26"}
    from_april = event("2025-06-20 09:43:26", action="config", source="2025-04.pdf", raw=raw)
    from_june = event("2025-06-20 09:43:26", action="config", source="2025-06.pdf", raw=raw)

    kept, removed = deduplicate([from_april, from_june])

    assert len(kept) == 1
    assert Path(kept[0].source_file).name == "2025-06.pdf"
    assert len(removed) == 1
    assert Path(removed[0].source_file).name == "2025-04.pdf"


def test_preference_ignores_input_order() -> None:
    """입력 순서가 바뀌어도 같은 사본이 남아야 한다."""
    raw = {"Action": "Setting changed"}
    april = event("2025-06-20 09:43:26", source="2025-04.pdf", raw=raw)
    june = event("2025-06-20 09:43:26", source="2025-06.pdf", raw=raw)

    for order in ([april, june], [june, april]):
        kept, _ = deduplicate(order)
        assert Path(kept[0].source_file).name == "2025-06.pdf"


def test_falls_back_to_month_share_when_filename_has_no_clue() -> None:
    """파일명에 단서가 없으면 그 달이 많이 든 파일 쪽을 남긴다."""
    raw = {"Action": "x"}
    focused = [event("2025-06-01 09:00:00", source="export_b.pdf", raw={"Action": f"n{i}"})
               for i in range(5)]
    mixed = [event("2025-01-01 09:00:00", source="export_a.pdf", raw={"Action": f"m{i}"})
             for i in range(20)]
    target_a = event("2025-06-20 09:43:26", source="export_a.pdf", raw=raw)
    target_b = event("2025-06-20 09:43:26", source="export_b.pdf", raw=raw)

    kept, removed = deduplicate([*mixed, *focused, target_a, target_b])

    survivor = next(e for e in kept if e.timestamp.day == 20)
    assert Path(survivor.source_file).name == "export_b.pdf"
    assert len(removed) == 1


def test_overlap_summary_reports_both_sides() -> None:
    raw = {"Action": "Setting changed"}
    april = event("2025-06-20 09:43:26", source="2025-04.pdf", raw=raw)
    june = event("2025-06-20 09:43:26", source="2025-06.pdf", raw=raw)

    kept, removed = deduplicate([april, june])
    lines = overlap_summary(removed, kept)

    assert len(lines) == 1
    assert "2025-04.pdf" in lines[0] and "2025-06.pdf" in lines[0]


# --------------------------------------------------------------------------
# 정렬
# --------------------------------------------------------------------------


def test_sort_is_chronological() -> None:
    events = [
        event("2026-01-05 09:00:00", raw={"n": "3"}),
        event("2024-07-01 08:31:23", raw={"n": "1"}),
        event("2025-06-20 09:43:26", raw={"n": "2"}),
    ]
    assert [e.raw["n"] for e in sort_events(events)] == ["1", "2", "3"]


def test_sort_is_stable_for_same_timestamp() -> None:
    same = "2025-06-20 09:43:26"
    events = [
        event(same, actor="b", source="2025-06.pdf", raw={"n": "2"}),
        event(same, actor="a", source="2025-06.pdf", raw={"n": "1"}),
    ]
    assert [e.actor for e in sort_events(events)] == ["a", "b"]
