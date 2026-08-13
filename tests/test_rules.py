"""위반 규칙 검증.

규칙은 공통 스키마만 본다 — 여기 테스트에도 장비 이름이 나오지 않는다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.core.rules import (
    RULES,
    RuleError,
    after_hours_change,
    audit_trail_disabled,
    delete_without_reason,
    evaluate,
    load_rules,
    repeated_login_failure,
    system_clock_changed,
)
from src.core.schema import AuditEvent

from .conftest import PROJECT_ROOT

RULES_FILE = PROJECT_ROOT / "config" / "rules" / "default.yaml"


def event(
    when: str = "2026-01-02 10:00:00",
    *,
    action: str = "modify",
    actor: str = "op01",
    target: str = "",
    old: str | None = None,
    new: str | None = None,
    reason: str | None = None,
    raw: dict | None = None,
) -> AuditEvent:
    return AuditEvent(
        timestamp=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
        actor=actor,
        action=action,
        target=target,
        old_value=old,
        new_value=new,
        reason=reason,
        equipment_id="demo",
        source_file="x.pdf",
        raw=raw or {},
    )


# --------------------------------------------------------------------------
# delete_without_reason
# --------------------------------------------------------------------------


def test_delete_without_reason_flags_missing_reason() -> None:
    found = list(delete_without_reason([event(action="delete", target="Peak Table")], {}))
    assert len(found) == 1
    assert "Peak Table" in found[0].evidence


def test_delete_with_reason_is_clean() -> None:
    assert not list(
        delete_without_reason([event(action="delete", reason="재검토 후 삭제")], {})
    )


@pytest.mark.parametrize("blank", ["", "  ", "-", "N/A"])
def test_blank_reason_markers_still_count_as_missing(blank: str) -> None:
    """'값 없음'의 표기가 장비마다 달라도 규칙은 하나로 봐야 한다."""
    assert list(delete_without_reason([event(action="delete", reason=blank)], {}))


def test_non_delete_is_ignored() -> None:
    assert not list(delete_without_reason([event(action="modify")], {}))


# --------------------------------------------------------------------------
# after_hours_change
# --------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [22, 23, 0, 3, 5])
def test_after_hours_covers_midnight_wrap(hour: int) -> None:
    when = f"2026-01-02 {hour:02d}:30:00"
    assert list(after_hours_change([event(when, action="modify")], {}))


@pytest.mark.parametrize("hour", [6, 9, 13, 21])
def test_business_hours_are_clean(hour: int) -> None:
    when = f"2026-01-02 {hour:02d}:30:00"
    assert not list(after_hours_change([event(when, action="modify")], {}))


def test_after_hours_only_watches_configured_actions() -> None:
    night = event("2026-01-02 23:30:00", action="login")
    assert not list(after_hours_change([night], {}))


def test_after_hours_window_is_configurable() -> None:
    noon = event("2026-01-02 12:00:00", action="modify")
    params = {"start_hour": 11, "end_hour": 13}
    assert list(after_hours_change([noon], params))


# --------------------------------------------------------------------------
# audit_trail_disabled
# --------------------------------------------------------------------------


def test_audit_disabled_detected_in_target_and_value() -> None:
    hit = event(action="config", target="Audit Trail", old="Enabled", new="Disabled")
    assert list(audit_trail_disabled([hit], {}))


def test_audit_disabled_detected_in_raw_column_name() -> None:
    """'Audit Trail' 이 값이 아니라 컬럼명 쪽에 있는 장비가 있다."""
    hit = event(action="config", raw={"Audit Trail": "off"})
    assert list(audit_trail_disabled([hit], {}))


def test_audit_mentioned_but_not_disabled_is_clean() -> None:
    ok = event(action="config", target="Audit Trail", old="Disabled", new="Enabled")
    # 'disabled' 가 이전값에 있으면 켠 것이므로 걸리면 안 된다
    assert not list(audit_trail_disabled([ok], {}))


def test_unrelated_off_setting_is_clean() -> None:
    ok = event(action="config", target="Beeper", new="Off")
    assert not list(audit_trail_disabled([ok], {}))


# --------------------------------------------------------------------------
# system_clock_changed
# --------------------------------------------------------------------------


def test_clock_change_detected_from_value_shape() -> None:
    """낱말이 아니라 값의 성질로 판단하므로 장비가 뭐라고 부르든 걸린다."""
    hit = event(
        "2025-06-20 09:43:26",
        action="config",
        target="Date / time",
        old="20.Jun.2025 09:43:26",
        new="29.Apr.2025 16:42:24",
    )
    found = list(system_clock_changed([hit], {}))
    assert len(found) == 1
    assert "뒤로" in found[0].evidence


def test_clock_change_forward_is_labelled() -> None:
    hit = event(
        action="config",
        old="29.Apr.2025 11:11:26",
        new="20.Jun.2025 11:03:29",
    )
    assert "앞으로" in next(iter(system_clock_changed([hit], {}))).evidence


def test_small_clock_drift_is_ignored() -> None:
    tiny = event(
        action="config",
        old="04.Feb.2025 15:08:07",
        new="04.Feb.2025 15:07:25",
    )
    assert not list(system_clock_changed([tiny], {}))


def test_non_time_values_are_ignored() -> None:
    other = event(action="config", target="Module factor", old="1.000", new="2.000")
    assert not list(system_clock_changed([other], {}))


# --------------------------------------------------------------------------
# repeated_login_failure
# --------------------------------------------------------------------------


def _failures(times: list[str], actor: str = "op01") -> list[AuditEvent]:
    return [event(t, action="login_failed", actor=actor) for t in times]


def test_three_failures_in_window_is_reported_once() -> None:
    found = list(
        repeated_login_failure(
            _failures(
                [
                    "2026-01-02 10:00:00",
                    "2026-01-02 10:05:00",
                    "2026-01-02 10:10:00",
                ]
            ),
            {},
        )
    )
    assert len(found) == 1
    assert "op01" in found[0].evidence


def test_failures_spread_beyond_window_are_clean() -> None:
    assert not list(
        repeated_login_failure(
            _failures(
                [
                    "2026-01-02 10:00:00",
                    "2026-01-02 11:00:00",
                    "2026-01-02 12:00:00",
                ]
            ),
            {},
        )
    )


def test_failures_of_different_people_are_not_combined() -> None:
    events = (
        _failures(["2026-01-02 10:00:00"], actor="a")
        + _failures(["2026-01-02 10:01:00"], actor="b")
        + _failures(["2026-01-02 10:02:00"], actor="c")
    )
    assert not list(repeated_login_failure(events, {}))


def test_successful_login_does_not_count() -> None:
    events = _failures(["2026-01-02 10:00:00", "2026-01-02 10:01:00"])
    events.append(event("2026-01-02 10:02:00", action="login"))
    assert not list(repeated_login_failure(events, {}))


def test_threshold_below_two_is_rejected() -> None:
    with pytest.raises(RuleError):
        list(repeated_login_failure([], {"threshold": 1}))


# --------------------------------------------------------------------------
# 설정 로딩
# --------------------------------------------------------------------------


def test_shipped_rules_file_loads() -> None:
    specs = load_rules(RULES_FILE)
    assert specs
    for spec in specs:
        assert spec.name in RULES
        assert spec.severity in ("high", "medium", "low")
        assert spec.description


def test_every_registered_rule_is_configured() -> None:
    """규칙을 코드에만 추가하고 YAML 에 안 넣으면 조용히 안 돌게 된다."""
    configured = {s.name for s in load_rules(RULES_FILE)}
    assert configured == set(RULES)


def test_unknown_rule_name_is_rejected(tmp_path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n  없는규칙:\n    severity: high\n", encoding="utf-8")
    with pytest.raises(RuleError):
        load_rules(path)


def test_bad_severity_is_rejected(tmp_path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n  delete_without_reason:\n    severity: 매우높음\n", encoding="utf-8"
    )
    with pytest.raises(RuleError):
        load_rules(path)


def test_disabled_rule_is_skipped(tmp_path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "  delete_without_reason:\n    enabled: false\n    severity: high\n"
        "  after_hours_change:\n    severity: low\n",
        encoding="utf-8",
    )
    specs = load_rules(path)
    assert [s.name for s in specs] == ["after_hours_change"]


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------


def test_evaluate_applies_severity_and_sorts_by_it() -> None:
    events = [
        event("2026-01-02 23:30:00", action="modify"),          # medium
        event("2026-01-03 10:00:00", action="delete"),          # high
    ]
    findings = evaluate(events, load_rules(RULES_FILE))
    assert [f.severity for f in findings] == ["high", "medium"]
    assert all(f.description for f in findings)
