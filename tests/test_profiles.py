"""모든 장비 프로파일에 대한 구조 검증.

여기 있는 검사는 데이터 파일이 없어도 돈다.
새 장비 YAML 을 넣으면 아무것도 안 해도 이 테스트들이 자동으로 그 장비를 검사한다.
"""

from __future__ import annotations

import pytest

from src.core.profile import SCHEMA_SOURCES, Profile, ProfileError
from src.core.schema import ACTIONS

from .conftest import PROFILE_DIR, all_profiles

PROFILES = all_profiles()
IDS = [p.id for p in PROFILES]


def test_profiles_exist() -> None:
    assert PROFILES, f"프로파일이 하나도 없습니다: {PROFILE_DIR}"


def test_ids_are_unique() -> None:
    assert len(IDS) == len(set(IDS)), f"id 중복: {IDS}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_id_matches_filename(profile: Profile) -> None:
    """파일명과 id 를 맞춰둔다 — 에러 메시지의 id 만 보고 파일을 찾을 수 있어야 한다."""
    assert profile.source is not None
    assert profile.id == profile.source.stem


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_detect_is_specific(profile: Profile) -> None:
    """판별 조건이 없으면 아무 파일에나 걸린다.

    DB 는 본문을 글자로 훑을 수 없어 테이블 이름으로 판별한다.
    """
    assert profile.detect_tokens, "판별 조건이 비어 있습니다"
    assert all(t.strip() for t in profile.detect_tokens)


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_required_mapping(profile: Profile) -> None:
    for required in ("timestamp", "actor", "action"):
        assert required in profile.mapping, f"map.{required} 누락"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_action_vocabulary_is_in_common_schema(profile: Profile) -> None:
    """장비 용어가 공통 어휘 밖으로 나가면 규칙이 장비를 알아야만 동작하게 된다."""
    table = profile.vocabulary.get("action", {})
    assert table, "action 어휘 매핑이 비어 있으면 원본 값이 그대로 흘러간다"
    unknown = {k: v for k, v in table.items() if v not in ACTIONS}
    assert not unknown, f"허용되지 않는 action 값: {unknown}"


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_output_columns_resolve(profile: Profile) -> None:
    """출력 컬럼이 존재하지 않는 원본 필드를 가리키면 영원히 빈 칸이 된다.

    blocks 형식은 read.fields 에 원본 필드가 모두 선언돼 있으므로 대조할 수 있다.
    """
    if not profile.output_columns:
        pytest.skip("output 미정의 — 기본 컬럼 사용")
    if profile.read_format != "blocks":
        pytest.skip("delimited 는 파일 헤더를 봐야 알 수 있어 파싱 테스트에서 검증한다")

    for column in profile.output_columns:
        if column.source in SCHEMA_SOURCES:
            continue
        assert column.source in profile.block_fields, (
            f"출력 컬럼 '{column.name}' 이 read.fields 에 없는 "
            f"'{column.source}' 를 가리킵니다"
        )


@pytest.mark.parametrize("profile", PROFILES, ids=IDS)
def test_mapped_columns_are_declared(profile: Profile) -> None:
    """blocks 는 헤더가 없으므로, 매핑 대상이 read.fields 에 선언돼 있어야 한다."""
    if profile.read_format != "blocks":
        pytest.skip("delimited 는 파일 헤더로 검증한다")
    for field_name, spec in profile.mapping.items():
        assert spec["column"] in profile.block_fields, (
            f"map.{field_name} 이 read.fields 에 없는 '{spec['column']}' 를 가리킵니다"
        )


def test_detect_conditions_do_not_collide() -> None:
    """두 프로파일의 판별 조건이 완전히 같으면 어떤 파일이든 반드시 중복 매칭된다."""
    seen: dict[frozenset[str], str] = {}
    for profile in PROFILES:
        key = frozenset(t.lower() for t in profile.detect_tokens)
        assert key not in seen, (
            f"판별 조건이 '{seen.get(key)}' 와 완전히 같습니다: {profile.id}"
        )
        seen[key] = profile.id


# --------------------------------------------------------------------------
# 로더가 잘못된 YAML 을 확실히 거부하는지
# --------------------------------------------------------------------------

BAD_PROFILES = {
    "id 없음": {"detect": {"header_contains": ["a"]}, "map": {"timestamp": {"column": "t"}}},
    "detect 없음": {"id": "x", "map": {"timestamp": {"column": "t"}}},
    "map 필수 누락": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "map": {"timestamp": {"column": "t"}},
    },
    "알 수 없는 map 필드": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "c"},
            "메모": {"column": "n"},
        },
    },
    "허용 밖 action": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "c"},
        },
        "vocabulary": {"action": {"Foo": "approve"}},
    },
    "delimiter 두 글자": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "read": {"delimiter": "||"},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "c"},
        },
    },
    "blocks 인데 fields 없음": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "read": {"format": "blocks"},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "c"},
        },
    },
    "map 이 fields 밖을 가리킴": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "read": {"format": "blocks", "fields": ["t", "a"]},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "없는키"},
        },
    },
    "잘못된 정규식": {
        "id": "x",
        "detect": {"header_contains": ["a"]},
        "map": {
            "timestamp": {"column": "t"},
            "actor": {"column": "a"},
            "action": {"column": "c", "pattern": "([unclosed"},
        },
    },
}


@pytest.mark.parametrize("raw", BAD_PROFILES.values(), ids=list(BAD_PROFILES))
def test_bad_profile_is_rejected(raw: dict) -> None:
    with pytest.raises(ProfileError):
        Profile.from_dict(raw)
