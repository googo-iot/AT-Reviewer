"""픽스처 기반 파싱 검증.

tests/fixtures/<프로파일id>/ 안에
  - 입력 파일 하나 이상 (.csv / .tsv / .txt / .pdf)
  - expected.csv  (그 프로파일의 출력 컬럼 그대로)
를 두면 자동으로 테스트 대상이 된다. Python 코드는 건드릴 필요가 없다.

폴더 이름이 곧 기대하는 프로파일 id 이므로, 판별이 엉뚱하게 되면 여기서 잡힌다.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.core.export import OUTPUT_ENCODING, write_profile_csv
from src.core.registry import Registry

from .conftest import FixtureCase, all_fixture_cases

CASES = all_fixture_cases()
IDS = [str(c) for c in CASES]

pytestmark = pytest.mark.skipif(not CASES, reason="픽스처가 없습니다")


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding=OUTPUT_ENCODING, newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), [dict(r) for r in reader]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_fixture_is_detected_as_expected_profile(
    case: FixtureCase, registry: Registry
) -> None:
    """폴더 이름의 프로파일로 정확히 판별되어야 한다 (0개도 2개도 아니고)."""
    profile = registry.identify(case.input_path)
    assert profile.id == case.profile_id


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_fixture_parses_without_errors(case: FixtureCase, registry: Registry) -> None:
    profile = registry.identify(case.input_path)
    result = profile.parse_file(case.input_path)
    assert not result.errors, "\n".join(str(e) for e in result.errors)
    assert result.events, "이벤트가 한 건도 나오지 않았습니다"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_fixture_matches_expected_table(
    case: FixtureCase, registry: Registry, tmp_path: Path
) -> None:
    """실제 산출물이 expected.csv 와 한 칸도 다르지 않아야 한다."""
    profile = registry.identify(case.input_path)
    result = profile.parse_file(case.input_path)

    produced = tmp_path / "produced.csv"
    write_profile_csv(profile, result.events, produced)

    expected_columns, expected_rows = _read_csv(case.expected_path)
    actual_columns, actual_rows = _read_csv(produced)

    assert actual_columns == expected_columns, "컬럼 구성이 다릅니다"
    assert len(actual_rows) == len(expected_rows), (
        f"행 수가 다릅니다: 실제 {len(actual_rows)} / 기대 {len(expected_rows)}"
    )
    for index, (actual, expected) in enumerate(zip(actual_rows, expected_rows), start=1):
        assert actual == expected, f"{index}번째 행이 다릅니다"
