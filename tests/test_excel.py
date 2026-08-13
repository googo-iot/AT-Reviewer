"""Excel 출력 검증."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.core.excel import Block, ExcelError, Sheet, write_workbook

load_workbook = pytest.importorskip("openpyxl").load_workbook


def simple_sheet(name: str = "표") -> Sheet:
    return Sheet(
        name,
        [
            Block(
                columns=("이름", "값"),
                rows=[{"이름": "가", "값": 1}, {"이름": "나", "값": 2}],
            )
        ],
    )


def test_writes_columns_and_rows(tmp_path) -> None:
    path = write_workbook([simple_sheet()], tmp_path / "결과.xlsx")
    sheet = load_workbook(path)["표"]

    assert [c.value for c in sheet[1]] == ["이름", "값"]
    assert [c.value for c in sheet[2]] == ["가", 1]
    assert sheet.max_row == 3


def test_single_table_gets_freeze_and_filter(tmp_path) -> None:
    """행이 수천 줄이라 머리글 고정과 필터가 없으면 보기 어렵다."""
    sheet = load_workbook(write_workbook([simple_sheet()], tmp_path / "a.xlsx"))["표"]
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:B3"


def test_multiple_blocks_are_separated_and_titled(tmp_path) -> None:
    workbook_sheet = Sheet(
        "요약",
        [
            Block(title="첫 표", columns=("a",), rows=[{"a": 1}]),
            Block(title="둘째 표", columns=("b",), rows=[{"b": 2}]),
        ],
    )
    sheet = load_workbook(write_workbook([workbook_sheet], tmp_path / "b.xlsx"))["요약"]
    values = [row[0].value for row in sheet.iter_rows(min_col=1, max_col=1)]

    assert "첫 표" in values and "둘째 표" in values
    # 표가 여럿이면 고정·필터를 걸지 않는다 (오히려 방해된다)
    assert sheet.freeze_panes is None
    assert sheet.auto_filter.ref is None


def test_formula_looking_value_is_not_executed(tmp_path) -> None:
    """감사추적 값이 Excel 수식으로 해석되어 다른 값으로 보이면 안 된다."""
    sheet = Sheet("표", [Block(columns=("값",), rows=[{"값": "=1+1"}])])
    written = load_workbook(write_workbook([sheet], tmp_path / "c.xlsx"))["표"]
    assert written["A2"].value == "'=1+1"


def test_none_becomes_blank_not_the_word_none(tmp_path) -> None:
    sheet = Sheet("표", [Block(columns=("값",), rows=[{"값": None}])])
    written = load_workbook(write_workbook([sheet], tmp_path / "d.xlsx"))["표"]
    assert written["A2"].value in (None, "")


def test_illegal_sheet_name_is_cleaned(tmp_path) -> None:
    sheet = Sheet("전체/이벤트[2026]", [Block(columns=("a",), rows=[])])
    workbook = load_workbook(write_workbook([sheet], tmp_path / "e.xlsx"))
    name = workbook.sheetnames[0]
    assert not set(name) & set(r"[]:*?/\\")
    assert len(name) <= 31


def test_duplicate_sheet_names_are_made_unique(tmp_path) -> None:
    workbook = load_workbook(
        write_workbook([simple_sheet("표"), simple_sheet("표")], tmp_path / "f.xlsx")
    )
    assert len(set(workbook.sheetnames)) == 2


def test_long_sheet_name_is_truncated(tmp_path) -> None:
    sheet = Sheet("전체이벤트_" + "가" * 40, [Block(columns=("a",), rows=[])])
    workbook = load_workbook(write_workbook([sheet], tmp_path / "g.xlsx"))
    assert len(workbook.sheetnames[0]) <= 31


def test_empty_table_still_has_header(tmp_path) -> None:
    """0건이어도 헤더는 남긴다 — '실패'인지 '해당 없음'인지 구분되어야 한다."""
    sheet = Sheet("위반내역", [Block(columns=("심각도", "규칙"), rows=[])])
    written = load_workbook(write_workbook([sheet], tmp_path / "h.xlsx"))["위반내역"]
    assert [c.value for c in written[1]] == ["심각도", "규칙"]


def test_no_sheets_is_rejected(tmp_path) -> None:
    with pytest.raises(ExcelError):
        write_workbook([], tmp_path / "i.xlsx")


def test_datetime_is_written_as_datetime(tmp_path) -> None:
    when = datetime(2026, 1, 2, 3, 4, 5)
    sheet = Sheet("표", [Block(columns=("때",), rows=[{"때": when}])])
    written = load_workbook(write_workbook([sheet], tmp_path / "j.xlsx"))["표"]
    assert written["A2"].value == when
