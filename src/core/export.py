"""표 출력.

컬럼 구성은 장비마다 다르므로 이 모듈은 컬럼을 정하지 않는다.
프로파일이 정한 컬럼(Profile.columns / Profile.row_for)을 받아 쓰기만 한다.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from .profile import DEFAULT_OUTPUT_TIME_FORMAT, Profile
from .schema import AuditEvent

__all__ = [
    "FINDING_COLUMNS",
    "FAILURE_COLUMNS",
    "FailureRecord",
    "failure_rows",
    "finding_rows",
    "OUTPUT_ENCODING",
    "deduplicate",
    "identity",
    "overlap_summary",
    "sort_events",
    "write_failures_csv",
    "write_findings_csv",
    "write_profile_csv",
    "write_table_csv",
]

#: Excel 이 한글을 바로 읽도록 BOM 포함 UTF-8 로 쓴다.
OUTPUT_ENCODING: Final[str] = "utf-8-sig"


@dataclass(slots=True)
class FailureRecord:
    """변환하지 못한 것 하나. 파일 단위(판별/읽기)일 수도, 레코드 단위일 수도 있다."""

    source_file: str
    stage: str                  # detect | read | row
    message: str
    line: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 정리
# --------------------------------------------------------------------------


def sort_events(events: Iterable[AuditEvent]) -> list[AuditEvent]:
    """시간순 정렬. 같은 시각이면 파일·행위자 순으로 안정 정렬한다.

    여러 파일을 합치면 시간이 뒤섞이는데,
    시간순으로 봐야 '누가 언제 무엇을 했는지'가 한 줄기로 읽힌다.
    """
    return sorted(
        events,
        key=lambda e: (e.timestamp, e.equipment_id, e.source_file, e.actor),
    )


def identity(event: AuditEvent) -> tuple[Any, ...]:
    """'같은 사건'의 기준. 원본파일은 뺀다 — 파일이 달라도 같은 사건이면 하나다.

    원본 레코드까지 통째로 비교한다. 공통 스키마만으로 판정하면 안 되는 이유:
    action 이 정규화되면서 서로 다른 사건이 같아 보일 수 있다.
    예를 들어 같은 초에 찍힌 'Successful login' 과 'Logout' 은
    둘 다 login 이 되고 나머지 칸은 비어 있어 완전히 같은 이벤트로 보인다.
    실제로 지워질 뻔한 사례가 있었다.

    덜 지우는 쪽이 잘못 지우는 쪽보다 항상 낫다.
    """
    original = tuple(
        sorted(
            (str(k), str(v))
            for k, v in event.raw.items()
            if not str(k).startswith("__")     # 파서가 붙인 메타데이터 제외
        )
    )
    return (
        event.timestamp,
        event.equipment_id,
        event.actor,
        event.action,
        event.target,
        event.old_value,
        event.new_value,
        event.reason,
        original,
    )


def _month_share(events: Iterable[AuditEvent]) -> dict[str, Counter[str]]:
    """파일별로 '어느 달의 기록이 몇 건 들어있는지'."""
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        stats[Path(event.source_file).name][event.timestamp.strftime("%Y-%m")] += 1
    return stats


def _source_rank(event: AuditEvent, stats: dict[str, Counter[str]]) -> tuple[Any, ...]:
    """중복된 사본 중 어느 파일 것을 남길지 순위. 작을수록 우선.

    내보내기 구간이 서로 겹치면 같은 사건이 여러 파일에 들어간다.
    이때 '그 사건이 원래 속한 파일'을 남겨야 원본 추적이 맞는다.

      1. 파일명이 그 사건의 연-월을 담고 있으면 우선
         (2025-06 사건은 2025-06.pdf 것을 남긴다)
      2. 그 파일 안에서 해당 월이 차지하는 비중이 큰 쪽을 우선
         (여러 달을 뭉쳐 담은 파일보다 그 달 전용 파일을 남긴다)
      3. 그래도 같으면 파일명 순 — 실행할 때마다 결과가 달라지지 않게
    """
    path = Path(event.source_file)
    year_month = event.timestamp.strftime("%Y-%m")
    counts = stats.get(path.name, Counter())
    total = sum(counts.values()) or 1
    return (
        0 if year_month in path.stem else 1,
        -(counts.get(year_month, 0) / total),
        path.name,
    )


def deduplicate(
    events: Sequence[AuditEvent],
) -> tuple[list[AuditEvent], list[AuditEvent]]:
    """여러 파일에 중복 수록된 사건을 하나로 줄인다.

    반환: (남긴 이벤트, 제외한 이벤트)
    제외한 쪽도 돌려주는 이유: 무엇이 빠졌는지 눈으로 확인할 수 있어야 하기 때문.
    조용히 줄어드는 산출물이 가장 위험하다.
    """
    stats = _month_share(events)
    best: dict[tuple[Any, ...], AuditEvent] = {}
    order: dict[tuple[Any, ...], int] = {}

    for index, event in enumerate(events):
        key = identity(event)
        if key not in best:
            best[key] = event
            order[key] = index
        elif _source_rank(event, stats) < _source_rank(best[key], stats):
            best[key] = event

    kept_set = {id(e) for e in best.values()}
    kept = [best[k] for k in sorted(best, key=lambda k: order[k])]
    removed = [e for e in events if id(e) not in kept_set]
    return kept, removed


def overlap_summary(removed: Iterable[AuditEvent], kept: Sequence[AuditEvent]) -> list[str]:
    """어느 파일끼리 얼마나 겹쳤는지 사람이 읽을 문장으로."""
    kept_by_key = {identity(e): Path(e.source_file).name for e in kept}
    pairs: Counter[tuple[str, str]] = Counter()
    for event in removed:
        dropped = Path(event.source_file).name
        winner = kept_by_key.get(identity(event), "?")
        pairs[(dropped, winner)] += 1
    return [
        f"{dropped} 의 {count:,}건이 {winner} 와 중복 (→ {winner} 를 남김)"
        for (dropped, winner), count in pairs.most_common()
    ]


# --------------------------------------------------------------------------
# 쓰기
# --------------------------------------------------------------------------


def _prepare(path: Path | str) -> Path:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_table_csv(
    columns: Sequence[str],
    rows: Iterable[dict[str, Any]],
    path: Path | str,
    *,
    encoding: str = OUTPUT_ENCODING,
) -> int:
    """컬럼 목록과 행들을 CSV 로 쓴다. 쓴 행 수를 반환.

    행이 0건이어도 헤더만 있는 파일을 만든다
    — 산출물이 아예 없으면 '실패'인지 '해당 없음'인지 구분이 안 된다.
    """
    target = _prepare(path)
    written = 0
    with target.open("w", encoding=encoding, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            written += 1
    return written


def write_profile_csv(
    profile: Profile,
    events: Sequence[AuditEvent],
    path: Path | str,
    *,
    encoding: str = OUTPUT_ENCODING,
) -> int:
    """한 장비의 이벤트를 그 장비의 컬럼 구성대로 CSV 로 쓴다."""
    return write_table_csv(
        profile.columns(),
        (profile.row_for(e) for e in events),
        path,
        encoding=encoding,
    )


#: 위반내역 표의 컬럼. 규칙은 공통 스키마만 보므로 장비와 무관하게 고정이다.
FINDING_COLUMNS: Final[tuple[str, ...]] = (
    "심각도",
    "규칙",
    "설명",
    "근거",
    "발생일시",
    "장비",
    "행위자",
    "행위",
    "대상",
    "이전값",
    "변경값",
    "사유",
    "원본파일",
    "원본줄번호",
    "원본레코드",
)

#: 심각도 표기 — 산출물에서 눈에 바로 들어오도록.
_SEVERITY_LABEL: Final[dict[str, str]] = {
    "high": "높음",
    "medium": "보통",
    "low": "낮음",
}


def finding_rows(findings: Sequence[Any]) -> list[dict[str, Any]]:
    """위반 의심 목록을 표의 행들로. CSV 와 Excel 이 같은 것을 쓴다."""

    def row(finding: Any) -> dict[str, Any]:
        event: AuditEvent = finding.event
        raw = {k: v for k, v in event.raw.items() if not str(k).startswith("__")}
        return {
            "심각도": _SEVERITY_LABEL.get(finding.severity, finding.severity),
            "규칙": finding.rule,
            "설명": finding.description,
            "근거": finding.evidence,
            "발생일시": event.timestamp.strftime(DEFAULT_OUTPUT_TIME_FORMAT),
            "장비": event.equipment_id,
            "행위자": event.actor,
            "행위": event.action,
            "대상": event.target,
            "이전값": event.old_value or "",
            "변경값": event.new_value or "",
            "사유": event.reason or "",
            "원본파일": Path(event.source_file).name if event.source_file else "",
            "원본줄번호": event.raw.get("__line__", ""),
            "원본레코드": json.dumps(raw, ensure_ascii=False, default=str),
        }

    return [row(f) for f in findings]


def write_findings_csv(
    findings: Sequence[Any],
    path: Path | str,
    *,
    encoding: str = OUTPUT_ENCODING,
) -> int:
    """위반 의심 목록을 CSV 로 쓴다. 0건이어도 헤더는 남긴다."""
    return write_table_csv(
        FINDING_COLUMNS, finding_rows(findings), path, encoding=encoding
    )


FAILURE_COLUMNS: Final[tuple[str, ...]] = ("원본파일", "단계", "줄번호", "사유", "원본레코드")


def failure_rows(failures: Sequence[FailureRecord]) -> list[dict[str, Any]]:
    return [
        {
            "원본파일": item.source_file,
            "단계": item.stage,
            "줄번호": item.line if item.line is not None else "",
            "사유": " ".join(item.message.split()),
            "원본레코드": (
                json.dumps(
                    {k: v for k, v in item.raw.items() if not str(k).startswith("__")},
                    ensure_ascii=False,
                    default=str,
                )
                if item.raw
                else ""
            ),
        }
        for item in failures
    ]


def write_failures_csv(
    failures: Sequence[FailureRecord],
    path: Path | str,
    *,
    encoding: str = OUTPUT_ENCODING,
) -> int:
    """변환 실패 목록을 별도 CSV 로 남긴다. 실패가 없으면 파일을 만들지 않는다."""
    if not failures:
        return 0
    return write_table_csv(
        FAILURE_COLUMNS, failure_rows(failures), path, encoding=encoding
    )
