"""공통 감사추적 스키마.

이 모듈의 존재 이유:
    장비마다 CSV 컬럼명도, 인코딩도, 용어도 전부 다르다.
    그 차이는 전부 프로파일(YAML) 단계에서 흡수하고,
    이 아래로는 오직 AuditEvent 하나만 흘러다닌다.
    따라서 위반 규칙(rules.py)은 장비 종류를 전혀 몰라도 된다.

이 모듈은 의도적으로 표준 라이브러리에만 의존한다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Final

__all__ = [
    "ACTIONS",
    "AuditEvent",
    "SchemaError",
    "normalize_action",
]


# --------------------------------------------------------------------------
# 허용 어휘
# --------------------------------------------------------------------------

#: action 필드가 가질 수 있는 값. 프로파일의 vocabulary 매핑 결과가 여기에 들어온다.
#:
#: 어휘를 늘릴 때의 기준: '위반 규칙이 이 값만 보고 판단할 수 있는가'.
#: 예를 들어 로그인 성공과 실패를 한 값으로 묶으면
#: '연속 로그인 실패' 규칙이 공통 스키마만으로는 불가능해진다.
ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "modify",        # 값 변경
        "delete",        # 삭제
        "create",        # 생성 (사용자·레코드 등)
        "login",         # 로그인 성공 / 로그아웃
        "login_failed",  # 로그인 실패 — 성공과 섞으면 침입 시도 탐지가 불가능해진다
        "sign",          # 전자서명 / 승인
        "config",        # 설정·시스템 변경
        "execute",       # 시험·분석 실행 (대기/시작/완료/중단)
        "backup",        # 백업 / 복원
        "operate",       # 조작 (HMI 버튼 누름, 화면 전환 등)
        "alarm",         # 설비 알람 (도어 열림, 비상정지, 모터 트립 등)
    }
)


class SchemaError(ValueError):
    """AuditEvent 생성에 실패했을 때 발생. 어느 파일/행인지까지 담아서 올린다."""


def normalize_action(value: Any) -> str:
    """action 후보값을 공통 어휘로 정규화한다.

    프로파일의 vocabulary가 이미 매핑을 끝냈다는 전제이며,
    여기서는 공백/대소문자만 흡수한다. 매핑되지 않은 값은 통과시키지 않는다.
    """
    text = "" if value is None else str(value).strip().lower()
    if text not in ACTIONS:
        allowed = ", ".join(sorted(ACTIONS))
        raise SchemaError(
            f"알 수 없는 action 값입니다: {value!r} "
            f"(허용: {allowed}) — 프로파일의 vocabulary.action 매핑을 확인하세요."
        )
    return text


def _clean(value: Any) -> str | None:
    """빈 문자열·공백·NaN 표기를 전부 None으로 통일한다.

    '값이 없음'의 표현이 장비마다 제각각이라(빈칸, '-', 'N/A', 'nan')
    규칙 쪽에서 매번 분기하지 않도록 여기서 한 번에 처리한다.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "-", "--"}:
        return None
    return text


def _require(value: Any, field_name: str) -> str:
    text = _clean(value)
    if text is None:
        raise SchemaError(f"필수 항목 '{field_name}'이(가) 비어 있습니다.")
    return text


# --------------------------------------------------------------------------
# 공통 스키마
# --------------------------------------------------------------------------


@dataclass(kw_only=True, slots=True)
class AuditEvent:
    """모든 장비의 감사추적 한 줄을 표현하는 공통 단위.

    kw_only 이므로 항상 키워드로 생성한다. 필드 순서가 바뀌어도 호출부가 깨지지 않는다.
    """

    timestamp: datetime          # 필수 — 발생 시각
    actor: str                   # 필수 — 행위자
    action: str                  # 필수 — ACTIONS 중 하나
    target: str = ""             # 대상 객체명
    old_value: str | None = None
    new_value: str | None = None
    reason: str | None = None
    equipment_id: str = ""       # 어느 장비(프로파일)에서 왔는지
    source_file: str = ""        # 원본 파일 경로
    raw: dict[str, Any] = field(default_factory=dict)  # 원본 행 전체 보존

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise SchemaError(
                f"timestamp는 datetime이어야 합니다: {self.timestamp!r} "
                f"({type(self.timestamp).__name__})"
            )
        self.actor = _require(self.actor, "actor")
        self.action = normalize_action(self.action)
        self.target = _clean(self.target) or ""
        self.old_value = _clean(self.old_value)
        self.new_value = _clean(self.new_value)
        self.reason = _clean(self.reason)
        self.equipment_id = _clean(self.equipment_id) or ""
        self.source_file = _clean(self.source_file) or ""
        if not isinstance(self.raw, dict):
            raise SchemaError(f"raw는 dict여야 합니다: {type(self.raw).__name__}")

    # -- 규칙에서 쓰는 편의 속성 -------------------------------------------

    @property
    def has_reason(self) -> bool:
        """사유가 실질적으로 기재되어 있는지."""
        return self.reason is not None

    @property
    def hour(self) -> int:
        """0-23. 업무시간 외 판정에 쓴다."""
        return self.timestamp.hour

    def raw_values(self) -> str:
        """원본 행의 '값'만 소문자 한 줄로. 컬럼명은 뺀다.

        raw_text() 는 컬럼명까지 넣는데, 낱말로 판단하는 규칙에서는
        그것이 오탐을 만든다 — 'user_id' 라는 컬럼명 때문에 모든 행이
        '계정 관련'으로 보이는 식이다.
        """
        parts = [
            str(value)
            for key, value in self.raw.items()
            if not str(key).startswith("__") and value is not None
        ]
        return " ".join(parts).lower()

    def raw_text(self) -> str:
        """원본 행 전체를 소문자 한 줄로. 키워드 검색형 규칙이 사용한다.

        컬럼명도 함께 넣는다 — 'Audit Trail'이 값이 아니라 컬럼명 쪽에
        들어있는 장비가 있기 때문.
        """
        parts: list[str] = []
        for key, value in self.raw.items():
            if str(key).startswith("__"):
                continue  # 파서가 붙인 메타데이터(__line__ 등)는 검색 대상이 아니다
            parts.append(str(key))
            if value is not None:
                parts.append(str(value))
        return " ".join(parts).lower()

    # -- 출력 --------------------------------------------------------------

    def to_row(self) -> dict[str, Any]:
        """Excel 시트 한 행으로. raw는 JSON 문자열로 눌러서 담는다."""
        return {
            "발생시각": self.timestamp,
            "장비": self.equipment_id,
            "행위자": self.actor,
            "행위": self.action,
            "대상": self.target,
            "이전값": self.old_value,
            "변경값": self.new_value,
            "사유": self.reason,
            "원본파일": self.source_file,
            "원본행": json.dumps(self.raw, ensure_ascii=False, default=str),
        }

    def to_dict(self) -> dict[str, Any]:
        """raw까지 포함한 원형 그대로의 dict (디버깅/직렬화용)."""
        return asdict(self)

    def __str__(self) -> str:  # 로그·에러 메시지용
        return (
            f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] {self.equipment_id} "
            f"{self.actor} {self.action} {self.target}"
        )
