"""명령줄 인터페이스.

    python -m src.cli convert data -o output/감사추적.csv
    python -m src.cli check   data -o output/위반내역.csv
    python -m src.cli analyze data -o output/감사추적분석.xlsx
    python -m src.cli profiles
    python -m src.cli inspect data/알수없는파일.pdf

이 파일은 '어떻게 읽을지'를 전혀 모른다. 그건 전부 프로파일(YAML)의 일이다.
처리 절차는 core/pipeline.py 에 있고, 여기서는 화면에 보여주기만 한다.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .core.excel import Block, ExcelError, Sheet, write_workbook
from .core.export import (
    FAILURE_COLUMNS,
    FINDING_COLUMNS,
    FailureRecord,
    failure_rows,
    finding_rows,
    write_failures_csv,
    write_findings_csv,
    write_profile_csv,
)
from .core.pipeline import FileReport, RunResult, run
from .core.profile import Profile, ProfileError
from .core.registry import (
    Registry,
    count_nested_inputs,
    find_input_files,
)
from .core.rules import RuleError, RuleSpec, evaluate, load_rules
from .core.schema import AuditEvent

DEFAULT_PROFILE_DIR = "config/profiles"
DEFAULT_RULES = "config/rules/default.yaml"
DEFAULT_OUTPUT = "output/감사추적.csv"
DEFAULT_FINDINGS = "output/위반내역.csv"
DEFAULT_WORKBOOK = "output/감사추적분석.xlsx"

SEVERITY_LABEL = {"high": "높음", "medium": "보통", "low": "낮음"}


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


def _report_line(report: FileReport) -> str:
    if report.status == "detect_failed":
        return f"  [판별실패] {report.name}"
    if report.status == "read_failed":
        return f"  [읽기실패] {report.name}"
    flag = f"  ← {report.errors}건 실패" if report.errors else ""
    return f"  {report.name:<28} {report.profile_id:<18} {report.events:>6,}건{flag}"


# --------------------------------------------------------------------------
# 공통 준비
# --------------------------------------------------------------------------


def _prepare(args: argparse.Namespace, with_rules: bool = False):
    """레지스트리·규칙·입력 파일을 준비한다. 실패하면 (None, 종료코드)."""
    try:
        registry = Registry.from_directory(args.profiles)
        specs = load_rules(args.rules) if with_rules else []
    except (ProfileError, RuleError) as exc:
        _say(f"설정을 불러오지 못했습니다:\n{exc}")
        return None, 2

    try:
        files = find_input_files(args.path, recursive=args.recursive)
    except FileNotFoundError as exc:
        _say(str(exc))
        return None, 2
    if not files:
        _say(f"처리할 파일이 없습니다: {Path(args.path).resolve()}")
        return None, 2

    head = f"프로파일 {len(registry)}개"
    if with_rules:
        head += f" / 규칙 {len(specs)}개"
    _say(f"{head} / 입력 {len(files)}개")
    if not args.recursive:
        nested = count_nested_inputs(args.path)
        if nested:
            _say(f"  (하위 폴더의 {nested}개는 제외했습니다. 포함하려면 -r)")
    _say("")

    return (registry, specs, files), 0


def _execute(args: argparse.Namespace, registry: Registry, files: list[Path]) -> RunResult:
    return run(
        files,
        registry,
        dedupe=not args.keep_duplicates,
        on_file=lambda report: _say(_report_line(report)),
    )


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------


def _output_path(base: Path, equipment_id: str, many: bool) -> Path:
    """장비가 하나면 지정한 경로 그대로, 여럿이면 장비별로 나눈다.

    장비마다 컬럼 구성이 다르므로 한 파일에 억지로 합치지 않는다.
    """
    return base if not many else base.with_name(f"{base.stem}_{equipment_id}{base.suffix}")


def _suffixed(base: Path, label: str, equipment_id: str, many: bool) -> Path:
    stem = f"{base.stem}_{equipment_id}" if many else base.stem
    return base.with_name(f"{stem}_{label}{base.suffix}")


def _write_csv(
    profile: Profile, events: list[AuditEvent], target: Path, encoding: str
) -> int | None:
    """CSV 하나를 쓴다. 실패하면 이유를 사람 말로 알리고 None 을 돌려준다."""
    try:
        return write_profile_csv(profile, events, target, encoding=encoding)
    except PermissionError:
        # 거의 항상 Excel 이 산출물을 붙잡고 있는 경우다.
        _say(f"\n산출물에 쓸 수 없습니다: {target}")
        _say("  파일이 Excel 등에서 열려 있는지 확인하고 닫은 뒤 다시 실행하세요.")
        _say("  (다른 이름으로 뽑으려면 -o 로 경로를 지정하세요.)")
    except OSError as exc:
        _say(f"\n산출물을 쓰지 못했습니다: {target}\n  {exc}")
    return None


def cmd_convert(args: argparse.Namespace) -> int:
    prepared, code = _prepare(args)
    if prepared is None:
        return code
    registry, _specs, files = prepared

    result = _execute(args, registry, files)
    if not result.total_events:
        _say("\n변환된 이벤트가 없습니다.")
        _write_failures(args, result.failures)
        return 1

    base = Path(args.output)
    many = len(result.per_equipment) > 1
    written: list[tuple[str, int, Path]] = []

    dropped_by_equipment: dict[str, list[AuditEvent]] = defaultdict(list)
    for event in result.removed:
        dropped_by_equipment[event.equipment_id].append(event)

    for equipment_id, events in result.per_equipment.items():
        profile = result.profiles[equipment_id]
        dropped = dropped_by_equipment.get(equipment_id, [])
        if dropped:
            # 빠진 것을 눈으로 확인할 수 있어야 한다. 조용히 줄어들면 안 된다.
            # 다만 부속 파일이라, 못 쓰더라도 본 산출물까지 막지는 않는다.
            path = _suffixed(base, "중복", equipment_id, many)
            if _write_csv(profile, dropped, path, args.encoding) is None:
                _say("  → 중복 목록은 저장하지 못했지만 변환은 계속합니다.")
            else:
                _say(f"\n중복 제외 {len(dropped):,}건 → {path}")

        target = _output_path(base, equipment_id, many)
        count = _write_csv(profile, events, target, args.encoding)
        if count is None:
            return 2
        written.append((equipment_id, count, target))

    _print_convert_summary(result, written)
    _write_failures(args, result.failures)
    return 0 if result.failed_files == 0 else 1


def _write_failures(args: argparse.Namespace, failures: list[FailureRecord]) -> None:
    if not failures:
        return
    base = Path(args.output)
    path = Path(args.errors) if args.errors else base.with_name(
        f"{base.stem}_실패{base.suffix}"
    )
    try:
        write_failures_csv(failures, path, encoding=args.encoding)
        _say(f"\n실패 내역 {len(failures)}건 → {path}")
    except OSError as exc:
        _say(f"\n실패 내역을 쓰지 못했습니다: {path} ({exc})")


def _print_convert_summary(result: RunResult, written: list[tuple[str, int, Path]]) -> None:
    events = result.events
    _say("\n" + "─" * 62)
    _say(
        f"파일      {result.ok_files}/{result.total_files} 처리"
        + (f" (실패 {result.failed_files})" if result.failed_files else "")
    )
    _say(
        f"이벤트    {len(events):,}건"
        + (f" (레코드 실패 {result.row_failures:,})" if result.row_failures else "")
    )
    if result.skipped:
        _say(f"무시      {result.skipped:,}건 (페이지 머리글 등 데이터가 아닌 블록)")
    if result.removed:
        _say(f"중복제거  {len(result.removed):,}건 (내보내기 구간이 겹치는 파일)")
        for line in result.overlaps:
            _say(f"          {line}")
    if events:
        _say(f"기간      {min(e.timestamp for e in events):%Y-%m-%d} ~ "
             f"{max(e.timestamp for e in events):%Y-%m-%d}")
        _say(f"행위자    {len({e.actor for e in events})}명")
        counts = Counter(e.action for e in events)
        _say("행위      " + ", ".join(f"{a} {n:,}" for a, n in counts.most_common()))
    _say("─" * 62)
    for equipment_id, count, path in written:
        _say(f"  {equipment_id}  {count:,}건  →  {path}")


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    prepared, code = _prepare(args, with_rules=True)
    if prepared is None:
        return code
    registry, specs, files = prepared

    result = _execute(args, registry, files)
    events = result.events
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
    _write_failures(args, result.failures)
    return 0 if result.failed_files == 0 else 1


def _print_findings_summary(
    events: list[AuditEvent],
    findings: list,
    specs: list[RuleSpec],
    target: Path,
) -> None:
    _say("\n" + "─" * 62)
    _say(f"검사 대상  {len(events):,}건")
    _say(f"위반 의심  {len(findings):,}건")

    by_severity = Counter(f.severity for f in findings)
    if findings:
        _say("심각도     " + ", ".join(
            f"{label} {by_severity[key]:,}"
            for key, label in SEVERITY_LABEL.items()
            if by_severity[key]
        ))
    _say("")
    by_rule = Counter(f.rule for f in findings)
    for spec in sorted(specs, key=lambda s: (s.severity, s.name)):
        count = by_rule.get(spec.name, 0)
        _say(f"  {' ' if count else '·'} {spec.name:<24} {spec.description:<18} {count:>5,}건")

    # 무거운 것부터 몇 건은 콘솔에서 바로 보이게 한다.
    top = [f for f in findings if f.severity == "high"][:8]
    if top:
        _say("\n  [높음] 상위 건")
        for finding in top:
            _say(f"    {finding.event.timestamp:%Y-%m-%d %H:%M}  "
                 f"{finding.event.actor:<28} {finding.evidence}")
        remaining = by_severity["high"] - len(top)
        if remaining > 0:
            _say(f"    … 외 {remaining:,}건")

    _say("─" * 62)
    _say(f"  위반내역  →  {target}")
    _say("  ※ 검증되지 않은 개인 분석 도구입니다. 최종 판정은 원본 확인이 필요합니다.")


# --------------------------------------------------------------------------
# analyze — Excel 한 권
# --------------------------------------------------------------------------


def build_sheets(
    result: RunResult,
    findings: list,
    specs: list[RuleSpec],
    source: str,
    recursive: bool,
) -> list[Sheet]:
    """Excel 시트 구성. GUI 도 이 함수를 그대로 쓴다."""
    events = result.events
    sheets = [
        Sheet("위반내역", [Block(columns=FINDING_COLUMNS, rows=finding_rows(findings))])
    ]
    many = len(result.per_equipment) > 1
    for equipment_id, group in result.per_equipment.items():
        profile = result.profiles[equipment_id]
        # 장비마다 컬럼이 다르므로 한 시트에 억지로 합치지 않는다.
        name = f"전체이벤트_{equipment_id}" if many else "전체이벤트"
        sheets.append(
            Sheet(name, [Block(
                columns=profile.columns(),
                rows=[profile.row_for(e) for e in group],
            )])
        )
    sheets.append(_summary_sheet(result, events, findings, specs, source, recursive))
    if result.failures:
        sheets.append(
            Sheet("실패내역", [Block(
                columns=FAILURE_COLUMNS, rows=failure_rows(result.failures)
            )])
        )
    return sheets


def _summary_sheet(
    result: RunResult,
    events: list[AuditEvent],
    findings: list,
    specs: list[RuleSpec],
    source: str,
    recursive: bool,
) -> Sheet:
    """처리요약 시트. 산출물만 보고도 '무엇을 어떻게 처리했는지' 알 수 있어야 한다."""
    def when(value: datetime | None) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""

    run_info = [
        ("대상 경로", str(Path(source).resolve())),
        ("실행 시각", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("하위 폴더 포함", "예" if recursive else "아니오"),
        ("처리 파일", f"{result.ok_files:,} / {result.total_files:,}"),
        ("이벤트", f"{len(events):,}건"),
        ("중복 제외", f"{len(result.removed):,}건"),
        ("무시(머리글 등)", f"{result.skipped:,}건"),
        ("변환 실패", f"{result.row_failures:,}건"),
        ("기간", f"{when(min((e.timestamp for e in events), default=None))} ~ "
                 f"{when(max((e.timestamp for e in events), default=None))}"),
        ("행위자", f"{len({e.actor for e in events})}명"),
        ("위반 의심", f"{len(findings):,}건"),
        ("주의", "검증되지 않은 개인 분석 도구입니다. 최종 판정은 원본 확인이 필요합니다."),
    ]
    by_rule = Counter(f.rule for f in findings)
    actions = Counter(e.action for e in events)

    return Sheet("처리요약", [
        Block(title="실행 정보", columns=("항목", "값"),
              rows=[{"항목": k, "값": v} for k, v in run_info]),
        Block(
            title="파일별 처리 결과",
            columns=("원본파일", "프로파일", "이벤트", "레코드실패", "무시", "첫 기록", "마지막 기록"),
            rows=[{
                "원본파일": r.name,
                "프로파일": r.profile_id or "(판별 실패)",
                "이벤트": r.events,
                "레코드실패": r.errors,
                "무시": r.skipped,
                "첫 기록": when(r.first),
                "마지막 기록": when(r.last),
            } for r in result.reports],
        ),
        Block(
            title="규칙별 위반 의심",
            columns=("규칙", "설명", "심각도", "건수"),
            rows=[{
                "규칙": s.name,
                "설명": s.description,
                "심각도": SEVERITY_LABEL.get(s.severity, s.severity),
                "건수": by_rule.get(s.name, 0),
            } for s in specs],
        ),
        Block(title="행위 분포", columns=("행위", "건수"),
              rows=[{"행위": a, "건수": n} for a, n in actions.most_common()]),
    ])


def cmd_analyze(args: argparse.Namespace) -> int:
    prepared, code = _prepare(args, with_rules=True)
    if prepared is None:
        return code
    registry, specs, files = prepared

    result = _execute(args, registry, files)
    events = result.events
    if not events:
        _say("\n변환된 이벤트가 없습니다.")
        return 1

    try:
        findings = evaluate(events, specs)
    except RuleError as exc:
        _say(f"\n규칙 실행 오류: {exc}")
        return 2

    sheets = build_sheets(result, findings, specs, args.path, args.recursive)
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
    return 0 if result.failed_files == 0 else 1


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
        _say(f"  [{'O' if report.is_match else ' '}] {report.describe()}")

    if args.text:
        from .core.profile import extract_text

        try:
            text, how = extract_text(path)
        except ProfileError as exc:
            _say(f"\n본문을 읽지 못했습니다: {exc}")
            return 2
        _say(f"\n--- 원본 본문 앞 {args.text}줄 ({how}) ---")
        for line in text.splitlines()[: args.text]:
            _say(f"  {line}")
    return 0


# --------------------------------------------------------------------------
# 진입점
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, rules: bool) -> None:
    parser.add_argument("path", help="파일 또는 폴더 경로")
    parser.add_argument("--profiles", default=DEFAULT_PROFILE_DIR, help="프로파일 디렉터리")
    if rules:
        parser.add_argument("--rules", default=DEFAULT_RULES, help="규칙 설정 YAML")
    parser.add_argument("--errors", default=None, help="실패 내역 CSV 경로")
    parser.add_argument("--encoding", default="utf-8-sig", help="산출물 인코딩")
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="하위 폴더까지 훑음 (기본: 지정한 폴더만)"
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="파일 간 중복을 제거하지 않고 전부 남김",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="제조장비 audit trail 을 공통 표로 변환합니다. "
        "검증되지 않은 개인 분석 도구이며 최종 판정은 원본 확인이 필요합니다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("convert", help="파일/폴더를 표(CSV)로 변환")
    _add_common(convert, rules=False)
    convert.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help=f"기본: {DEFAULT_OUTPUT}")
    convert.set_defaults(func=cmd_convert, rules=DEFAULT_RULES)

    check = sub.add_parser("check", help="변환 후 위반 의심 항목 검사")
    _add_common(check, rules=True)
    check.add_argument("-o", "--output", default=DEFAULT_FINDINGS, help=f"기본: {DEFAULT_FINDINGS}")
    check.set_defaults(func=cmd_check)

    analyze = sub.add_parser(
        "analyze", help="변환 + 검사를 Excel 한 권으로 (시트: 위반내역/전체이벤트/처리요약)"
    )
    _add_common(analyze, rules=True)
    analyze.add_argument("-o", "--output", default=DEFAULT_WORKBOOK, help=f"기본: {DEFAULT_WORKBOOK}")
    analyze.set_defaults(func=cmd_analyze)

    profiles = sub.add_parser("profiles", help="등록된 장비 프로파일 목록")
    profiles.add_argument("--profiles", default=DEFAULT_PROFILE_DIR)
    profiles.set_defaults(func=cmd_profiles)

    inspect = sub.add_parser("inspect", help="이 파일이 어느 프로파일에 걸리는지 진단")
    inspect.add_argument("file", help="검사할 파일")
    inspect.add_argument("--profiles", default=DEFAULT_PROFILE_DIR)
    inspect.add_argument(
        "--text",
        nargs="?",
        type=int,
        const=40,
        default=0,
        help="원본 본문 앞 N줄을 함께 출력 (새 장비 YAML 을 쓸 때 필요, 기본 40)",
    )
    inspect.set_defaults(func=cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
