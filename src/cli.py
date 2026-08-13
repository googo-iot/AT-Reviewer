"""명령줄 인터페이스.

    python -m src.cli convert data -o output/감사추적.csv
    python -m src.cli profiles
    python -m src.cli inspect data/알수없는파일.pdf

이 파일은 '어떻게 읽을지'를 전혀 모른다. 그건 전부 프로파일(YAML)의 일이다.
여기서 하는 일은 파일 모으기 → 판별 → 파싱 → 쓰기 → 요약뿐이다.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .core.excel import Block, ExcelError, Sheet, write_workbook
from .core.export import (
    FAILURE_COLUMNS,
    FINDING_COLUMNS,
    FailureRecord,
    failure_rows,
    finding_rows,
    deduplicate,
    overlap_summary,
    sort_events,
    write_failures_csv,
    write_findings_csv,
    write_profile_csv,
)
from .core.profile import Profile, ProfileError
from .core.rules import RuleError, evaluate, load_rules
from .core.registry import (
    DetectionError,
    Registry,
    count_nested_inputs,
    find_input_files,
)
from .core.schema import AuditEvent

DEFAULT_PROFILE_DIR = "config/profiles"
DEFAULT_RULES = "config/rules/default.yaml"
DEFAULT_OUTPUT = "output/감사추적.csv"
DEFAULT_FINDINGS = "output/위반내역.csv"
DEFAULT_WORKBOOK = "output/감사추적분석.xlsx"


# --------------------------------------------------------------------------
# 콘솔
# --------------------------------------------------------------------------


def _setup_console() -> None:
    """Windows 콘솔은 기본이 cp949 라 한글 산출물 경로에서 깨진다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _say(message: str = "") -> None:
    print(message)


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------


@dataclass(slots=True)
class FileReport:
    """파일 하나의 처리 결과 요약. 처리요약 시트의 한 행이 된다."""

    name: str
    profile_id: str
    events: int = 0
    errors: int = 0
    skipped: int = 0
    first: datetime | None = None
    last: datetime | None = None


@dataclass(slots=True)
class Collected:
    """입력 전체를 읽은 결과."""

    by_equipment: dict[str, list[AuditEvent]] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)
    reports: list[FileReport] = field(default_factory=list)
    ok_files: int = 0
    skipped: int = 0


def _collect(registry: Registry, files: list[Path]) -> Collected:
    """파일들을 읽어 장비별 이벤트로 모은다.

    한 파일이 실패해도 나머지는 계속 처리한다 — 그래야 한 번 돌려서
    '무엇이 되고 무엇이 안 되는지'를 한꺼번에 볼 수 있다.
    """
    by_equipment: dict[str, list[AuditEvent]] = defaultdict(list)
    profiles: dict[str, Profile] = {}
    failures: list[FailureRecord] = []
    reports: list[FileReport] = []
    ok_files = 0
    skipped = 0

    for path in files:
        try:
            profile, result = registry.read(path)
        except DetectionError as exc:
            failures.append(
                FailureRecord(source_file=path.name, stage="detect", message=str(exc))
            )
            _say(f"  [판별실패] {path.name}")
            continue
        except ProfileError as exc:            # 읽기 자체가 안 되거나 컬럼이 안 맞는 경우
            failures.append(
                FailureRecord(source_file=path.name, stage="read", message=str(exc))
            )
            _say(f"  [읽기실패] {path.name}")
            continue
        finally:
            # 본문은 더 필요 없다. 파일이 많을 때 통째로 붙들고 있지 않는다.
            registry.forget(path)

        ok_files += 1
        skipped += result.skipped
        profiles[profile.id] = profile
        by_equipment[profile.id].extend(result.events)
        for row_error in result.errors:
            failures.append(
                FailureRecord(
                    source_file=path.name,
                    stage="row",
                    message=row_error.message,
                    line=row_error.line,
                    raw=row_error.raw,
                )
            )
        times = [e.timestamp for e in result.events]
        reports.append(
            FileReport(
                name=path.name,
                profile_id=profile.id,
                events=len(result.events),
                errors=len(result.errors),
                skipped=result.skipped,
                first=min(times) if times else None,
                last=max(times) if times else None,
            )
        )
        flag = "" if result.ok else f"  ← {len(result.errors)}건 실패"
        _say(f"  {path.name:<28} {profile.id:<18} {len(result.events):>6,}건{flag}")

    return Collected(
        by_equipment=by_equipment,
        profiles=profiles,
        failures=failures,
        reports=reports,
        ok_files=ok_files,
        skipped=skipped,
    )


def _output_path(base: Path, equipment_id: str, many: bool) -> Path:
    """장비가 하나면 지정한 경로 그대로, 여럿이면 장비별로 나눈다.

    장비마다 컬럼 구성이 다르므로 한 파일에 억지로 합치지 않는다.
    """
    if not many:
        return base
    return base.with_name(f"{base.stem}_{equipment_id}{base.suffix}")


def _write(
    profile: Profile,
    events: list[AuditEvent],
    target: Path,
    args: argparse.Namespace,
) -> int | None:
    """CSV 하나를 쓴다. 실패하면 이유를 사람 말로 알리고 None 을 돌려준다."""
    try:
        return write_profile_csv(profile, events, target, encoding=args.encoding)
    except PermissionError:
        # 거의 항상 Excel 이 산출물을 붙잡고 있는 경우다.
        _say(f"\n산출물에 쓸 수 없습니다: {target}")
        _say("  파일이 Excel 등에서 열려 있는지 확인하고 닫은 뒤 다시 실행하세요.")
        _say("  (다른 이름으로 뽑으려면 -o 로 경로를 지정하세요.)")
    except OSError as exc:
        _say(f"\n산출물을 쓰지 못했습니다: {target}\n  {exc}")
    return None


def _suffixed(base: Path, label: str, equipment_id: str, many: bool) -> Path:
    """산출물 옆에 두는 부속 파일 경로 (_중복, _실패 등)."""
    stem = f"{base.stem}_{equipment_id}" if many else base.stem
    return base.with_name(f"{stem}_{label}{base.suffix}")


def cmd_convert(args: argparse.Namespace) -> int:
    try:
        registry = Registry.from_directory(args.profiles)
    except ProfileError as exc:
        _say(f"프로파일을 불러오지 못했습니다:\n{exc}")
        return 2

    try:
        files = find_input_files(args.path, recursive=args.recursive)
    except FileNotFoundError as exc:
        _say(str(exc))
        return 2
    if not files:
        _say(f"처리할 파일이 없습니다: {Path(args.path).resolve()}")
        return 2

    _say(f"프로파일 {len(registry)}개 / 입력 {len(files)}개")
    if not args.recursive:
        nested = count_nested_inputs(args.path)
        if nested:
            _say(f"  (하위 폴더의 {nested}개는 제외했습니다. 포함하려면 -r)")
    _say("")
    collected = _collect(registry, files)
    by_equipment = collected.by_equipment
    profiles = collected.profiles
    failures = collected.failures
    ok_files = collected.ok_files
    skipped = collected.skipped

    total_events = sum(len(v) for v in by_equipment.values())
    if not total_events:
        _say("\n변환된 이벤트가 없습니다.")
        _write_failures(args, failures)
        return 1

    # -- 쓰기 --------------------------------------------------------------
    base = Path(args.output)
    many = len(by_equipment) > 1
    written: list[tuple[str, int, Path]] = []
    removed_total = 0
    overlaps: list[str] = []
    final: dict[str, list[AuditEvent]] = {}   # 요약은 실제로 쓴 것 기준이어야 한다

    for equipment_id, events in sorted(by_equipment.items()):
        events = sort_events(events)
        if not args.keep_duplicates:
            events, removed = deduplicate(events)
            removed_total += len(removed)
            overlaps.extend(overlap_summary(removed, events))
            if removed:
                # 빠진 것을 눈으로 확인할 수 있어야 한다. 조용히 줄어들면 안 된다.
                # 다만 이건 부속 파일이라, 못 쓰더라도 본 산출물까지 막지는 않는다.
                dropped_path = _suffixed(base, "중복", equipment_id, many)
                if _write(profiles[equipment_id], removed, dropped_path, args) is None:
                    _say("  → 중복 목록은 저장하지 못했지만 변환은 계속합니다.")
                else:
                    _say(f"\n중복 제외 {len(removed):,}건 → {dropped_path}")

        target = _output_path(base, equipment_id, many)
        count = _write(profiles[equipment_id], events, target, args)
        if count is None:
            return 2
        written.append((equipment_id, count, target))
        final[equipment_id] = events

    _print_summary(
        files=len(files),
        ok_files=ok_files,
        skipped=skipped,
        failures=failures,
        written=written,
        by_equipment=final,
        removed=removed_total,
        overlaps=overlaps,
    )
    _write_failures(args, failures)
    return 0 if ok_files == len(files) else 1


def _write_failures(args: argparse.Namespace, failures: list[FailureRecord]) -> None:
    if not failures:
        return
    path = Path(args.errors) if args.errors else Path(args.output).with_name(
        f"{Path(args.output).stem}_실패{Path(args.output).suffix}"
    )
    write_failures_csv(failures, path, encoding=args.encoding)
    _say(f"\n실패 내역 {len(failures)}건 → {path}")


def _print_summary(
    *,
    files: int,
    ok_files: int,
    skipped: int,
    failures: list[FailureRecord],
    written: list[tuple[str, int, Path]],
    by_equipment: dict[str, list[AuditEvent]],
    removed: int,
    overlaps: list[str],
) -> None:
    all_events = [e for events in by_equipment.values() for e in events]
    row_failures = sum(1 for f in failures if f.stage == "row")
    file_failures = len(failures) - row_failures

    _say("\n" + "─" * 62)
    _say(f"파일      {ok_files}/{files} 처리" + (f" (실패 {file_failures})" if file_failures else ""))
    _say(f"이벤트    {len(all_events):,}건" + (f" (레코드 실패 {row_failures:,})" if row_failures else ""))
    if skipped:
        _say(f"무시      {skipped:,}건 (페이지 머리글 등 데이터가 아닌 블록)")
    if removed:
        _say(f"중복제거  {removed:,}건 (내보내기 구간이 겹치는 파일)")
        for line in overlaps:
            _say(f"          {line}")

    if all_events:
        first = min(e.timestamp for e in all_events)
        last = max(e.timestamp for e in all_events)
        _say(f"기간      {first:%Y-%m-%d} ~ {last:%Y-%m-%d}")
        _say(f"행위자    {len({e.actor for e in all_events})}명")
        counts = Counter(e.action for e in all_events)
        _say("행위      " + ", ".join(f"{a} {n:,}" for a, n in counts.most_common()))

    _say("─" * 62)
    for equipment_id, count, path in written:
        _say(f"  {equipment_id}  {count:,}건  →  {path}")


# --------------------------------------------------------------------------
# check — 변환한 뒤 규칙을 적용한다
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    try:
        registry = Registry.from_directory(args.profiles)
        specs = load_rules(args.rules)
    except (ProfileError, RuleError) as exc:
        _say(f"설정을 불러오지 못했습니다:\n{exc}")
        return 2

    try:
        files = find_input_files(args.path, recursive=args.recursive)
    except FileNotFoundError as exc:
        _say(str(exc))
        return 2
    if not files:
        _say(f"처리할 파일이 없습니다: {Path(args.path).resolve()}")
        return 2

    _say(f"프로파일 {len(registry)}개 / 규칙 {len(specs)}개 / 입력 {len(files)}개\n")
    collected = _collect(registry, files)
    by_equipment = collected.by_equipment
    failures = collected.failures
    ok_files = collected.ok_files

    events: list[AuditEvent] = []
    for group in by_equipment.values():
        group = sort_events(group)
        if not args.keep_duplicates:
            group, _removed = deduplicate(group)
        events.extend(group)
    events = sort_events(events)

    if not events:
        _say("\n검사할 이벤트가 없습니다.")
        return 1

    try:
        findings = evaluate(events, specs)
    except RuleError as exc:
        _say(f"\n규칙 실행 오류: {exc}")
        return 2

    target = Path(args.output)
    try:
        write_findings_csv(findings, target, encoding=args.encoding)
    except PermissionError:
        _say(f"\n산출물에 쓸 수 없습니다: {target}")
        _say("  파일이 Excel 등에서 열려 있는지 확인하고 닫은 뒤 다시 실행하세요.")
        return 2

    _print_findings_summary(events, findings, specs, target)
    _write_failures(args, failures)
    return 0 if ok_files == len(files) else 1


def _print_findings_summary(
    events: list[AuditEvent],
    findings: list,
    specs: list,
    target: Path,
) -> None:
    _say("\n" + "─" * 62)
    _say(f"검사 대상  {len(events):,}건")
    _say(f"위반 의심  {len(findings):,}건")

    by_severity = Counter(f.severity for f in findings)
    if findings:
        _say(
            "심각도     "
            + ", ".join(
                f"{label} {by_severity[key]:,}"
                for key, label in (("high", "높음"), ("medium", "보통"), ("low", "낮음"))
                if by_severity[key]
            )
        )
    _say("")
    by_rule = Counter(f.rule for f in findings)
    for spec in sorted(specs, key=lambda s: (s.severity, s.name)):
        count = by_rule.get(spec.name, 0)
        mark = " " if count else "·"
        _say(f"  {mark} {spec.name:<24} {spec.description:<18} {count:>5,}건")

    # 무거운 것부터 몇 건은 콘솔에서 바로 보이게 한다.
    top = [f for f in findings if f.severity == "high"][:8]
    if top:
        _say("\n  [높음] 상위 건")
        for finding in top:
            _say(
                f"    {finding.event.timestamp:%Y-%m-%d %H:%M}  "
                f"{finding.event.actor:<28} {finding.evidence}"
            )
        remaining = by_severity["high"] - len(top)
        if remaining > 0:
            _say(f"    … 외 {remaining:,}건")

    _say("─" * 62)
    _say(f"  위반내역  →  {target}")
    _say("  ※ 검증되지 않은 개인 분석 도구입니다. 최종 판정은 원본 확인이 필요합니다.")


# --------------------------------------------------------------------------
# analyze — 변환 + 검사 결과를 Excel 한 권으로
# --------------------------------------------------------------------------


def _summary_sheet(
    args: argparse.Namespace,
    collected: Collected,
    events: list[AuditEvent],
    findings: list,
    specs: list,
    removed: int,
) -> Sheet:
    """처리요약 시트. 산출물만 보고도 '무엇을 어떻게 처리했는지' 알 수 있어야 한다."""
    when = lambda t: t.strftime("%Y-%m-%d %H:%M:%S") if t else ""  # noqa: E731

    run = [
        ("대상 경로", str(Path(args.path).resolve())),
        ("실행 시각", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("하위 폴더 포함", "예" if args.recursive else "아니오"),
        ("처리 파일", f"{collected.ok_files:,} / {collected.ok_files + sum(1 for f in collected.failures if f.stage != 'row'):,}"),
        ("이벤트", f"{len(events):,}건"),
        ("중복 제외", f"{removed:,}건"),
        ("무시(머리글 등)", f"{collected.skipped:,}건"),
        ("변환 실패", f"{sum(1 for f in collected.failures if f.stage == 'row'):,}건"),
        ("기간", f"{when(min((e.timestamp for e in events), default=None))} ~ "
                 f"{when(max((e.timestamp for e in events), default=None))}"),
        ("행위자", f"{len({e.actor for e in events})}명"),
        ("위반 의심", f"{len(findings):,}건"),
        ("주의", "검증되지 않은 개인 분석 도구입니다. 최종 판정은 원본 확인이 필요합니다."),
    ]

    by_rule = Counter(f.rule for f in findings)
    severity_label = {"high": "높음", "medium": "보통", "low": "낮음"}
    actions = Counter(e.action for e in events)

    return Sheet(
        name="처리요약",
        blocks=[
            Block(
                title="실행 정보",
                columns=("항목", "값"),
                rows=[{"항목": k, "값": v} for k, v in run],
            ),
            Block(
                title="파일별 처리 결과",
                columns=("원본파일", "프로파일", "이벤트", "레코드실패", "무시", "첫 기록", "마지막 기록"),
                rows=[
                    {
                        "원본파일": r.name,
                        "프로파일": r.profile_id,
                        "이벤트": r.events,
                        "레코드실패": r.errors,
                        "무시": r.skipped,
                        "첫 기록": when(r.first),
                        "마지막 기록": when(r.last),
                    }
                    for r in collected.reports
                ],
            ),
            Block(
                title="규칙별 위반 의심",
                columns=("규칙", "설명", "심각도", "건수"),
                rows=[
                    {
                        "규칙": s.name,
                        "설명": s.description,
                        "심각도": severity_label.get(s.severity, s.severity),
                        "건수": by_rule.get(s.name, 0),
                    }
                    for s in specs
                ],
            ),
            Block(
                title="행위 분포",
                columns=("행위", "건수"),
                rows=[{"행위": a, "건수": n} for a, n in actions.most_common()],
            ),
        ],
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    try:
        registry = Registry.from_directory(args.profiles)
        specs = load_rules(args.rules)
    except (ProfileError, RuleError) as exc:
        _say(f"설정을 불러오지 못했습니다:\n{exc}")
        return 2

    try:
        files = find_input_files(args.path, recursive=args.recursive)
    except FileNotFoundError as exc:
        _say(str(exc))
        return 2
    if not files:
        _say(f"처리할 파일이 없습니다: {Path(args.path).resolve()}")
        return 2

    _say(f"프로파일 {len(registry)}개 / 규칙 {len(specs)}개 / 입력 {len(files)}개")
    if not args.recursive:
        nested = count_nested_inputs(args.path)
        if nested:
            _say(f"  (하위 폴더의 {nested}개는 제외했습니다. 포함하려면 -r)")
    _say("")

    collected = _collect(registry, files)
    if not any(collected.by_equipment.values()):
        _say("\n변환된 이벤트가 없습니다.")
        return 1

    # -- 중복 정리 ---------------------------------------------------------
    removed_total = 0
    per_equipment: dict[str, list[AuditEvent]] = {}
    for equipment_id, group in sorted(collected.by_equipment.items()):
        group = sort_events(group)
        if not args.keep_duplicates:
            group, removed = deduplicate(group)
            removed_total += len(removed)
        per_equipment[equipment_id] = group

    events = sort_events([e for group in per_equipment.values() for e in group])

    try:
        findings = evaluate(events, specs)
    except RuleError as exc:
        _say(f"\n규칙 실행 오류: {exc}")
        return 2

    # -- 시트 구성 ---------------------------------------------------------
    sheets = [
        Sheet("위반내역", [Block(columns=FINDING_COLUMNS, rows=finding_rows(findings))])
    ]
    many = len(per_equipment) > 1
    for equipment_id, group in per_equipment.items():
        profile = collected.profiles[equipment_id]
        # 장비마다 컬럼이 다르므로 한 시트에 억지로 합치지 않는다.
        name = f"전체이벤트_{equipment_id}" if many else "전체이벤트"
        sheets.append(
            Sheet(
                name,
                [Block(columns=profile.columns(), rows=[profile.row_for(e) for e in group])],
            )
        )
    sheets.append(
        _summary_sheet(args, collected, events, findings, specs, removed_total)
    )
    if collected.failures:
        sheets.append(
            Sheet(
                "실패내역",
                [Block(columns=FAILURE_COLUMNS, rows=failure_rows(collected.failures))],
            )
        )

    target = Path(args.output)
    try:
        write_workbook(sheets, target)
    except PermissionError:
        _say(f"\n산출물에 쓸 수 없습니다: {target}")
        _say("  파일이 Excel 등에서 열려 있는지 확인하고 닫은 뒤 다시 실행하세요.")
        return 2
    except (ExcelError, OSError) as exc:
        _say(f"\nExcel 을 쓰지 못했습니다: {target}\n  {exc}")
        return 2

    _print_findings_summary(events, findings, specs, target)
    _say(f"  시트  {', '.join(s.name for s in sheets)}")
    return 0 if collected.ok_files == len(files) else 1


# --------------------------------------------------------------------------
# profiles / inspect
# --------------------------------------------------------------------------


def cmd_profiles(args: argparse.Namespace) -> int:
    try:
        registry = Registry.from_directory(args.profiles)
    except ProfileError as exc:
        _say(f"프로파일을 불러오지 못했습니다:\n{exc}")
        return 2
    _say(f"프로파일 {len(registry)}개 — {Path(args.profiles).resolve()}\n")
    for profile in registry:
        source = profile.source.name if profile.source else "?"
        _say(f"  {profile.id}  ({source})")
        _say(f"      이름     {profile.name}")
        _say(f"      형식     {profile.read_format}")
        _say(f"      판별조건 {list(profile.header_contains)}")
        _say(f"      출력컬럼 {list(profile.columns())}")
        _say("")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """어느 프로파일에 왜 걸리는지/안 걸리는지 보여준다. 새 장비를 붙일 때 쓴다."""
    try:
        registry = Registry.from_directory(args.profiles)
    except ProfileError as exc:
        _say(f"프로파일을 불러오지 못했습니다:\n{exc}")
        return 2

    path = Path(args.file)
    if not path.is_file():
        _say(f"파일을 찾을 수 없습니다: {path}")
        return 2

    _say(f"{path.name}\n")
    for report in registry.inspect(path):
        mark = "O" if report.is_match else " "
        _say(f"  [{mark}] {report.describe()}")
    return 0


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="제조장비 audit trail 을 공통 표로 변환합니다. "
        "검증되지 않은 개인 분석 도구이며 최종 판정은 원본 확인이 필요합니다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="파일/폴더를 표(CSV)로 변환")
    convert.add_argument("path", help="파일 또는 폴더 경로")
    convert.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, help=f"산출물 경로 (기본: {DEFAULT_OUTPUT})"
    )
    convert.add_argument(
        "--profiles", default=DEFAULT_PROFILE_DIR, help="프로파일 디렉터리"
    )
    convert.add_argument(
        "--errors", default=None, help="실패 내역 CSV 경로 (기본: 산출물 옆 _실패.csv)"
    )
    convert.add_argument(
        "--encoding", default="utf-8-sig", help="산출물 인코딩 (기본: utf-8-sig)"
    )
    convert.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="하위 폴더까지 훑음 (기본: 지정한 폴더만)",
    )
    convert.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="파일 간 중복을 제거하지 않고 전부 남김 (기본: 제거하고 _중복.csv 로 따로 보관)",
    )
    convert.set_defaults(func=cmd_convert)

    check = sub.add_parser("check", help="변환 후 위반 의심 항목 검사")
    check.add_argument("path", help="파일 또는 폴더 경로")
    check.add_argument(
        "-o", "--output", default=DEFAULT_FINDINGS, help=f"위반내역 경로 (기본: {DEFAULT_FINDINGS})"
    )
    check.add_argument("--profiles", default=DEFAULT_PROFILE_DIR, help="프로파일 디렉터리")
    check.add_argument("--rules", default=DEFAULT_RULES, help="규칙 설정 YAML")
    check.add_argument("--errors", default=None, help="실패 내역 CSV 경로")
    check.add_argument("--encoding", default="utf-8-sig")
    check.add_argument(
        "-r", "--recursive", action="store_true", help="하위 폴더까지 훑음"
    )
    check.add_argument("--keep-duplicates", action="store_true", help="중복을 제거하지 않음")
    check.set_defaults(func=cmd_check)

    analyze = sub.add_parser(
        "analyze", help="변환 + 검사 결과를 Excel 한 권으로 (시트: 위반내역/전체이벤트/처리요약)"
    )
    analyze.add_argument("path", help="파일 또는 폴더 경로")
    analyze.add_argument(
        "-o", "--output", default=DEFAULT_WORKBOOK, help=f"산출물 경로 (기본: {DEFAULT_WORKBOOK})"
    )
    analyze.add_argument("--profiles", default=DEFAULT_PROFILE_DIR, help="프로파일 디렉터리")
    analyze.add_argument("--rules", default=DEFAULT_RULES, help="규칙 설정 YAML")
    analyze.add_argument(
        "-r", "--recursive", action="store_true", help="하위 폴더까지 훑음"
    )
    analyze.add_argument("--keep-duplicates", action="store_true", help="중복을 제거하지 않음")
    analyze.set_defaults(func=cmd_analyze)

    profiles = sub.add_parser("profiles", help="등록된 장비 프로파일 목록")
    profiles.add_argument("--profiles", default=DEFAULT_PROFILE_DIR)
    profiles.set_defaults(func=cmd_profiles)

    inspect = sub.add_parser("inspect", help="이 파일이 어느 프로파일에 걸리는지 진단")
    inspect.add_argument("file", help="검사할 파일")
    inspect.add_argument("--profiles", default=DEFAULT_PROFILE_DIR)
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
