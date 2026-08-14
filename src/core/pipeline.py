"""처리 파이프라인.

파일 모으기 → 판별 → 파싱 → 중복 정리까지의 절차를 한 곳에 둔다.
CLI 와 GUI 가 같은 것을 쓴다. 화면에 어떻게 보여줄지만 각자 다르다.

진행 상황은 콜백으로 알린다. 이 모듈은 print 도 하지 않고 창도 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .export import FailureRecord, deduplicate, overlap_summary, sort_events
from .profile import Profile, ProfileError
from .registry import DetectionError, Registry
from .schema import AuditEvent

__all__ = ["FileReport", "RunResult", "equipment_label", "run"]


#: 설비명으로 보지 않는 폴더 이름. 여러 설비를 담는 통 폴더들이다.
GENERIC_FOLDERS: frozenset[str] = frozenset(
    {"data", "output", "logs", "log", "audit", "temp", "tmp",
     "backup", "downloads", "download", "desktop", "documents"}
)


def equipment_label(path: Path, fallback: str) -> str:
    """이 파일이 '어느 설비' 것인지 정한다. 파일이 든 폴더 이름을 쓴다.

    프로파일은 '이 형식을 어떻게 읽는가'(기종)이고, 설비는 '어느 호기인가'다.
    형식이 같은 설비가 여러 대면 이 둘이 갈라져야 한다.
    실제로 SCADA 두 대가 파일명(SystemLog.db)까지 같아 구분이 안 됐다.

        data/2026-01.pdf            → 'data' 는 통 폴더 → 프로파일 id
        data/FIT-I001/2026-01.pdf   → 'FIT-I001'
        data/ATM-I001/SystemLog.db  → 'ATM-I001'

    파일을 설비별 폴더에 나눠 두는 것이 이미 하고 있는 정리 방식이라
    따로 적어 넣을 것이 없다. 어느 경로를 지정하고 실행하든 결과가 같다
    — 지정한 위치에 따라 설비명이 달라지면 산출물을 믿을 수 없다.
    """
    folder = Path(path).parent.name
    if folder and folder.lower() not in GENERIC_FOLDERS:
        return folder
    return fallback


@dataclass(slots=True)
class FileReport:
    """파일 하나의 처리 결과."""

    name: str
    path: Path
    status: str                     # ok | detect_failed | read_failed
    profile_id: str = ""
    events: int = 0
    errors: int = 0
    skipped: int = 0
    first: datetime | None = None
    last: datetime | None = None
    message: str = ""               # 실패했을 때의 사유

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(slots=True)
class RunResult:
    """입력 전체를 처리한 결과."""

    per_equipment: dict[str, list[AuditEvent]] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)
    reports: list[FileReport] = field(default_factory=list)
    removed: list[AuditEvent] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    skipped: int = 0
    excluded: int = 0            # 행위 종류로 걸러낸 건수
    total_files: int = 0
    cancelled: bool = False

    @property
    def ok_files(self) -> int:
        return sum(1 for r in self.reports if r.ok)

    @property
    def failed_files(self) -> int:
        return sum(1 for r in self.reports if not r.ok)

    @property
    def events(self) -> list[AuditEvent]:
        """장비 구분 없이 시간순으로 합친 전체 이벤트."""
        return sort_events([e for group in self.per_equipment.values() for e in group])

    @property
    def total_events(self) -> int:
        return sum(len(group) for group in self.per_equipment.values())

    @property
    def row_failures(self) -> int:
        return sum(1 for f in self.failures if f.stage == "row")


def run(
    files: Sequence[Path],
    registry: Registry,
    *,
    dedupe: bool = True,
    exclude_actions: Iterable[str] = (),
    on_file: Callable[[FileReport], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RunResult:
    """파일들을 읽어 장비별 이벤트로 만든다.

    on_file:         파일 하나를 끝낼 때마다 호출. 실패한 파일도 포함해서 부른다.
    cancelled:       True 를 돌려주면 다음 파일로 넘어가지 않고 멈춘다.
    exclude_actions: 이 행위는 산출물에서 뺀다. 장비 종류와 무관하게
                     공통 어휘로 지정하므로 어떤 장비에나 같은 방식으로 걸린다.

    한 파일이 실패해도 나머지는 계속 처리한다 — 그래야 한 번 돌려서
    '무엇이 되고 무엇이 안 되는지'를 한꺼번에 볼 수 있다.
    """
    result = RunResult(total_files=len(files))
    grouped: dict[str, list[AuditEvent]] = {}
    unwanted = {a.strip().lower() for a in exclude_actions if a and a.strip()}

    for path in files:
        if cancelled is not None and cancelled():
            result.cancelled = True
            break

        report = _read_one(registry, path, grouped, result)
        result.reports.append(report)
        if on_file is not None:
            on_file(report)

    # -- 정리 --------------------------------------------------------------
    for equipment_id, group in sorted(grouped.items()):
        if unwanted:
            before = len(group)
            group = [e for e in group if e.action not in unwanted]
            result.excluded += before - len(group)
        group = sort_events(group)
        if dedupe:
            group, removed = deduplicate(group)
            if removed:
                result.removed.extend(removed)
                result.overlaps.extend(overlap_summary(removed, group))
        result.per_equipment[equipment_id] = group

    return result


def _read_one(
    registry: Registry,
    path: Path,
    grouped: dict[str, list[AuditEvent]],
    result: RunResult,
) -> FileReport:
    try:
        profile = registry.identify(path)
        label = equipment_label(path, profile.id)
        profile, parsed = registry.read(path, equipment_id=label)
    except DetectionError as exc:
        result.failures.append(
            FailureRecord(source_file=path.name, stage="detect", message=str(exc))
        )
        return FileReport(
            name=path.name, path=path, status="detect_failed", message=str(exc)
        )
    except ProfileError as exc:          # 읽기 자체가 안 되거나 컬럼이 안 맞는 경우
        result.failures.append(
            FailureRecord(source_file=path.name, stage="read", message=str(exc))
        )
        return FileReport(
            name=path.name, path=path, status="read_failed", message=str(exc)
        )
    finally:
        # 본문은 더 필요 없다. 파일이 많을 때 통째로 붙들고 있지 않는다.
        registry.forget(path)

    result.skipped += parsed.skipped
    # 산출물은 설비 단위로 나눈다. 읽는 규칙(프로파일)은 여러 설비가 공유할 수 있다.
    result.profiles[label] = profile
    grouped.setdefault(label, []).extend(parsed.events)
    for row_error in parsed.errors:
        result.failures.append(
            FailureRecord(
                source_file=path.name,
                stage="row",
                message=row_error.message,
                line=row_error.line,
                raw=row_error.raw,
            )
        )

    times = [e.timestamp for e in parsed.events]
    return FileReport(
        name=path.name,
        path=path,
        status="ok",
        profile_id=label,
        events=len(parsed.events),
        errors=len(parsed.errors),
        skipped=parsed.skipped,
        first=min(times) if times else None,
        last=max(times) if times else None,
    )
