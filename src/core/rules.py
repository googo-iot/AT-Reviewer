"""위반 의심 탐지 규칙.

규칙은 공통 스키마(AuditEvent)만 본다. 어느 장비에서 왔는지 알지 못하고,
알 필요도 없다. 장비별 차이는 전부 프로파일 단계에서 흡수됐다는 전제다.

규칙 하나당 함수 하나. 함수는 이벤트 목록을 받아 Finding 을 내놓는다.
목록 전체를 받는 이유: '30분 안에 로그인 실패 3회' 처럼
한 건만 봐서는 판단할 수 없는 규칙이 있기 때문이다.

임계값·키워드 같은 조정거리는 config/rules/default.yaml 에 있다.
규칙을 새로 만들 때만 이 파일을 건드린다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Iterator, Sequence

import yaml

from .schema import AuditEvent

__all__ = [
    "Finding",
    "account_created",
    "account_unlocked",
    "RULES",
    "RuleError",
    "RuleSpec",
    "SEVERITIES",
    "evaluate",
    "load_rules",
]

SEVERITIES: Final[tuple[str, ...]] = ("high", "medium", "low")
_SEVERITY_ORDER: Final[dict[str, int]] = {s: i for i, s in enumerate(SEVERITIES)}

#: 값이 시각인지 판단할 때 시도할 형식. 장비마다 표기가 달라 목록으로 둔다.
DEFAULT_TIME_FORMATS: Final[tuple[str, ...]] = (
    "%d.%b.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)


class RuleError(Exception):
    """규칙 설정이 잘못됐을 때."""


@dataclass(slots=True)
class Finding:
    """위반 의심 한 건. 왜 걸렸는지(근거)를 반드시 갖는다.

    근거가 없으면 사람이 원본을 다시 뒤져야 하고, 그러면 도구를 쓰는 의미가 없다.
    """

    rule: str
    severity: str
    description: str
    event: AuditEvent
    evidence: str = ""

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER.get(self.severity, len(SEVERITIES))


@dataclass(slots=True)
class RuleSpec:
    """YAML 에서 읽은 규칙 하나의 설정."""

    name: str
    check: Callable[[Sequence[AuditEvent], dict[str, Any]], Iterator[Finding]]
    severity: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 헬퍼
# --------------------------------------------------------------------------


def _as_lower_list(value: Any, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip().lower() for v in value if str(v).strip())


def _parse_time(text: str | None, formats: Sequence[str]) -> datetime | None:
    """값이 시각으로 읽히면 datetime, 아니면 None."""
    if not text:
        return None
    candidate = text.strip()
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _humanize(delta: timedelta) -> str:
    total = int(abs(delta).total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days}일" if days else "", f"{hours}시간" if hours else "",
             f"{minutes}분" if minutes else ""]
    body = " ".join(p for p in parts if p) or "1분 미만"
    return f"{body} {'뒤로' if delta.total_seconds() < 0 else '앞으로'}"


# --------------------------------------------------------------------------
# 규칙 — 하나당 함수 하나
# --------------------------------------------------------------------------


def delete_without_reason(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """삭제인데 사유가 비어 있다.

    사유 없는 삭제는 무엇이 왜 없어졌는지 사후에 확인할 방법이 없다.
    """
    for event in events:
        if event.action == "delete" and not event.has_reason:
            yield Finding(
                rule="delete_without_reason",
                severity="",
                description="",
                event=event,
                evidence=f"삭제 대상 '{event.target or '(대상 미기재)'}' / 사유 없음",
            )


def after_hours_change(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """업무시간 외에 일어난 변경·삭제.

    시작 시각이 종료 시각보다 크면 자정을 넘긴 구간으로 본다 (22시~06시).
    """
    start = int(params.get("start_hour", 22))
    end = int(params.get("end_hour", 6))
    actions = set(_as_lower_list(params.get("actions"), ("modify", "delete")))

    def outside(hour: int) -> bool:
        return hour >= start or hour < end if start > end else start <= hour < end

    for event in events:
        if event.action in actions and outside(event.hour):
            yield Finding(
                rule="after_hours_change",
                severity="",
                description="",
                event=event,
                evidence=(
                    f"{event.timestamp:%Y-%m-%d %H:%M} "
                    f"(업무시간 외 {start:02d}시~{end:02d}시 구간)"
                ),
            )


def audit_trail_disabled(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """감사추적 기능 자체를 끈 정황.

    대상 또는 원본 레코드에 '감사추적'을 가리키는 낱말과
    '끔'을 가리키는 낱말이 함께 있으면 걸린다.
    """
    subjects = _as_lower_list(params.get("subject_keywords"), ("audit", "감사추적"))
    states = _as_lower_list(
        params.get("state_keywords"), ("off", "disable", "disabled", "해제", "중지")
    )

    for event in events:
        haystack = f"{event.target} {event.new_value or ''} {event.raw_text()}".lower()
        if not any(word in haystack for word in subjects):
            continue
        hit = next((word for word in states if word in haystack), None)
        if hit:
            yield Finding(
                rule="audit_trail_disabled",
                severity="",
                description="",
                event=event,
                evidence=f"'{event.target}' 에서 '{hit}' 검출",
            )


def system_clock_changed(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """시스템 시각을 바꾼 기록.

    이전값과 변경값이 둘 다 시각으로 읽히고 차이가 크면 시각 조작으로 본다.
    낱말이 아니라 값의 성질로 판단하므로 장비가 뭐라고 부르든 걸린다.

    시각이 바뀌면 그 뒤의 모든 기록이 실제 순서와 어긋나므로,
    감사추적에서는 단일 사건 중 가장 무거운 축이다.
    """
    threshold = timedelta(minutes=float(params.get("min_shift_minutes", 5)))
    formats = tuple(params.get("time_formats") or DEFAULT_TIME_FORMATS)

    for event in events:
        before = _parse_time(event.old_value, formats)
        after = _parse_time(event.new_value, formats)
        if before is None or after is None:
            continue
        shift = after - before
        if abs(shift) < threshold:
            continue
        yield Finding(
            rule="system_clock_changed",
            severity="",
            description="",
            event=event,
            evidence=(
                f"{before:%Y-%m-%d %H:%M:%S} → {after:%Y-%m-%d %H:%M:%S} "
                f"({_humanize(shift)})"
            ),
        )


def account_created(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """계정을 새로 만든 기록.

    계정 생성은 곧 접근 권한 부여다. 정당한 절차를 거쳤는지,
    실제로 쓰이는 사람인지 확인이 필요하다.

    action 이 create 인 것만으로는 안 된다 — 같은 create 에
    프로그램 생성, 레시피 임포트 같은 것이 함께 들어온다.
    계정을 가리키는 낱말이 있을 때만 인정한다.
    """
    actions = set(_as_lower_list(params.get("actions"), ("create",)))
    subjects = _as_lower_list(
        params.get("subject_keywords"), ("user", "account", "계정", "사용자")
    )
    for event in events:
        if event.action not in actions:
            continue
        # 컬럼명은 빼고 값만 본다 (컬럼명에 user 가 있는 장비가 있다).
        haystack = f"{event.target} {event.raw_values()}".lower()
        if any(word in haystack for word in subjects):
            yield Finding(
                rule="account_created",
                severity="",
                description="",
                event=event,
                evidence=f"'{event.target or '(대상 미기재)'}' — 생성자 {event.actor}",
            )


def account_unlocked(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """잠긴 계정을 푼 기록.

    계정은 로그인을 여러 번 틀려야 잠긴다. 따라서 잠금 해제 기록은
    '그 앞에 실패가 있었다'는 흔적이다. 장비에 따라 실패 자체는
    남기지 않고 해제만 남기는 경우가 있어(SCADA 가 그렇다),
    이 규칙이 없으면 그 구간이 통째로 안 보인다.
    """
    release_words = _as_lower_list(
        params.get("keywords"), ("unlock", "잠금 해제", "잠금해제", "잠금해지")
    )
    # '해제' 낱말만 보면 도어 잠금 해제 같은 것까지 걸린다
    # (실제로 'DOORS UNLOCK 기능이 활성화 되었습니다' 가 10건 잡혔다).
    # 계정을 가리키는 낱말이 함께 있을 때만 인정한다.
    account_words = _as_lower_list(
        params.get("subject_keywords"),
        ("login", "account", "user", "id:", "계정", "사용자"),
    )
    for event in events:
        # 컬럼명은 빼고 값만 본다. raw_text() 를 쓰면 'user_id' 같은
        # 컬럼명 때문에 모든 행이 계정 관련으로 보인다.
        haystack = f"{event.target} {event.new_value or ''} {event.raw_values()}".lower()
        hit = next((word for word in release_words if word in haystack), None)
        if hit and any(word in haystack for word in account_words):
            yield Finding(
                rule="account_unlocked",
                severity="",
                description="",
                event=event,
                evidence=f"'{event.target or hit}' — 해제자 {event.actor}",
            )


def repeated_login_failure(
    events: Sequence[AuditEvent], params: dict[str, Any]
) -> Iterator[Finding]:
    """짧은 시간에 같은 사람 이름으로 로그인 실패가 반복된 구간.

    임계값에 도달한 그 시점의 이벤트만 보고한다
    — 실패 100건을 100줄로 늘어놓으면 정작 볼 것이 묻힌다.
    """
    threshold = int(params.get("threshold", 3))
    window = timedelta(minutes=float(params.get("window_minutes", 30)))
    if threshold < 2:
        raise RuleError("repeated_login_failure.threshold 는 2 이상이어야 합니다")

    recent: dict[str, deque[datetime]] = defaultdict(deque)
    for event in sorted(events, key=lambda e: e.timestamp):
        if event.action != "login_failed":
            continue
        seen = recent[event.actor]
        seen.append(event.timestamp)
        while seen and event.timestamp - seen[0] > window:
            seen.popleft()
        if len(seen) == threshold:          # 도달한 순간 한 번만
            yield Finding(
                rule="repeated_login_failure",
                severity="",
                description="",
                event=event,
                evidence=(
                    f"'{event.actor}' {int(window.total_seconds() // 60)}분 내 "
                    f"{threshold}회 실패 ({seen[0]:%Y-%m-%d %H:%M} ~ "
                    f"{event.timestamp:%H:%M})"
                ),
            )
            seen.clear()                    # 다음 묶음부터 다시 센다


#: 이름 → 함수. YAML 은 이 이름으로 규칙을 켜고 끈다.
RULES: Final[dict[str, Callable[..., Iterator[Finding]]]] = {
    "delete_without_reason": delete_without_reason,
    "after_hours_change": after_hours_change,
    "audit_trail_disabled": audit_trail_disabled,
    "account_created": account_created,
    "account_unlocked": account_unlocked,
    "system_clock_changed": system_clock_changed,
    "repeated_login_failure": repeated_login_failure,
}


# --------------------------------------------------------------------------
# 설정 로딩 / 실행
# --------------------------------------------------------------------------


def load_rules(path: Path | str = "config/rules/default.yaml") -> list[RuleSpec]:
    """규칙 설정 YAML 을 읽는다. 켜진 규칙만 돌려준다."""
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleError(f"규칙 설정이 없습니다: {target.resolve()}") from exc
    except yaml.YAMLError as exc:
        raise RuleError(f"규칙 설정 YAML 문법 오류: {target}\n{exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), dict):
        raise RuleError(f"최상위에 'rules' 매핑이 필요합니다: {target}")

    specs: list[RuleSpec] = []
    for name, entry in raw["rules"].items():
        if name not in RULES:
            raise RuleError(
                f"알 수 없는 규칙 '{name}' ({target})\n"
                f"  사용 가능: {', '.join(sorted(RULES))}"
            )
        entry = entry or {}
        if not isinstance(entry, dict):
            raise RuleError(f"rules.{name} 은 매핑이어야 합니다 ({target})")
        if not entry.get("enabled", True):
            continue
        severity = str(entry.get("severity", "medium")).strip().lower()
        if severity not in SEVERITIES:
            raise RuleError(
                f"rules.{name}.severity 는 {', '.join(SEVERITIES)} 중 하나여야 합니다: "
                f"{severity!r}"
            )
        description = str(entry.get("description") or name).strip()
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise RuleError(f"rules.{name}.params 는 매핑이어야 합니다 ({target})")
        specs.append(
            RuleSpec(
                name=name,
                check=RULES[name],
                severity=severity,
                description=description,
                params=params,
            )
        )
    if not specs:
        raise RuleError(f"켜져 있는 규칙이 없습니다: {target}")
    return specs


def evaluate(
    events: Sequence[AuditEvent], specs: Iterable[RuleSpec]
) -> list[Finding]:
    """규칙을 전부 돌려 위반 의심 목록을 만든다. 심각도 → 시간 순."""
    findings: list[Finding] = []
    for spec in specs:
        for finding in spec.check(events, spec.params):
            # 심각도와 설명은 YAML 이 정한다. 규칙 함수는 '무엇이 걸렸는지'만 안다.
            finding.severity = spec.severity
            finding.description = spec.description
            findings.append(finding)
    findings.sort(key=lambda f: (f.rank, f.event.timestamp, f.rule))
    return findings
