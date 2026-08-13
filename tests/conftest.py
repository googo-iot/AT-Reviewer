"""테스트 공통 설정.

핵심 원칙: 프로파일이 늘어나면 테스트도 저절로 늘어난다.
새 장비 YAML 을 넣으면 구조 검증은 자동으로 걸리고,
tests/fixtures/<프로파일id>/ 에 입력+기대값을 두면 파싱 검증까지 자동으로 걸린다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROFILE_DIR = PROJECT_ROOT / "config" / "profiles"
FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: 픽스처 폴더에서 입력 파일로 인정할 확장자.
INPUT_SUFFIXES = (".csv", ".tsv", ".txt", ".pdf")

#: 기대 결과 파일 이름.
EXPECTED_NAME = "expected.csv"

from src.core.profile import Profile, load_profiles  # noqa: E402
from src.core.registry import Registry  # noqa: E402


# --------------------------------------------------------------------------
# 수집 (parametrize 에 쓰려면 fixture 가 아니라 일반 함수여야 한다)
# --------------------------------------------------------------------------


def all_profiles() -> list[Profile]:
    """config/profiles/*.yaml 전부. 하나라도 깨지면 여기서 즉시 드러난다."""
    return load_profiles(PROFILE_DIR)


@dataclass(frozen=True)
class FixtureCase:
    """픽스처 한 벌: 입력 파일 + 기대 결과 CSV."""

    profile_id: str
    input_path: Path
    expected_path: Path

    def __str__(self) -> str:
        return f"{self.profile_id}/{self.input_path.name}"


def all_fixture_cases() -> list[FixtureCase]:
    """tests/fixtures/<프로파일id>/ 아래에서 입력+기대값 쌍을 찾아낸다.

    폴더 이름이 곧 기대하는 프로파일 id 다 — 판별이 엉뚱하게 되면 테스트가 잡는다.
    """
    if not FIXTURE_DIR.is_dir():
        return []
    cases: list[FixtureCase] = []
    for folder in sorted(p for p in FIXTURE_DIR.iterdir() if p.is_dir()):
        expected = folder / EXPECTED_NAME
        if not expected.is_file():
            continue
        for candidate in sorted(folder.iterdir()):
            if candidate.name == EXPECTED_NAME or not candidate.is_file():
                continue
            if candidate.suffix.lower() in INPUT_SUFFIXES:
                cases.append(
                    FixtureCase(
                        profile_id=folder.name,
                        input_path=candidate,
                        expected_path=expected,
                    )
                )
    return cases


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def registry() -> Registry:
    return Registry.from_directory(PROFILE_DIR)


@pytest.fixture(scope="session")
def profiles_by_id() -> dict[str, Profile]:
    return {p.id: p for p in all_profiles()}
