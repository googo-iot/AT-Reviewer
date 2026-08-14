"""설비 구분 검증.

프로파일은 '이 형식을 어떻게 읽는가'(기종)이고, 설비는 '어느 호기인가'다.
형식이 같은 설비가 여러 대일 때 이 둘이 갈라지지 않으면
산출물에서 어느 설비 기록인지 알 수 없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.pipeline import equipment_label


@pytest.mark.parametrize(
    "path, expected",
    [
        ("data/FIT-I001/2026-01.pdf", "FIT-I001"),
        ("data/ATM-I001/SystemLog.db", "ATM-I001"),
        ("C:/어디든/PKG-2호기/audit.csv", "PKG-2호기"),
    ],
)
def test_folder_name_becomes_equipment(path: str, expected: str) -> None:
    assert equipment_label(Path(path), "프로파일") == expected


@pytest.mark.parametrize(
    "path",
    [
        "data/2026-01.pdf",          # 여러 설비를 담는 통 폴더
        "output/SystemLog.db",
        "C:/작업/TEMP/x.csv",
    ],
)
def test_generic_folder_falls_back_to_profile(path: str) -> None:
    """'data' 같은 통 폴더 이름은 설비명이 아니다."""
    assert equipment_label(Path(path), "프로파일") == "프로파일"


def test_label_does_not_depend_on_where_you_point() -> None:
    """지정한 위치에 따라 설비명이 달라지면 산출물을 믿을 수 없다.

    폴더를 지정하든 파일 하나를 지정하든 같은 설비명이 나와야 한다.
    """
    target = Path("data/ATM-I001/SystemLog.db")
    assert equipment_label(target, "프로파일") == "ATM-I001"
    assert equipment_label(target.resolve(), "프로파일") == "ATM-I001"


def test_same_format_two_machines_are_separated() -> None:
    """형식도 파일명도 같은 두 설비가 갈려야 한다 (실제로 겪은 상황)."""
    a = equipment_label(Path("data/SystemLog.db"), "scada_systemlog")
    b = equipment_label(Path("data/ATM-I001/SystemLog.db"), "scada_systemlog")
    assert a != b
