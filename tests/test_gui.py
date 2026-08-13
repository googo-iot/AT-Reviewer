"""GUI 검증.

창을 띄우지 않고, 실제 처리를 맡는 Worker 만 직접 돌린다.
창 조립 자체는 화면이 있는 환경에서만 확인한다.
"""

from __future__ import annotations

import queue
from pathlib import Path

import pytest

from src.gui import Worker

from .conftest import FIXTURE_DIR

SOURCE = FIXTURE_DIR / "pall_flowstar_v"

pytestmark = pytest.mark.skipif(
    not SOURCE.is_dir(), reason="픽스처가 없습니다"
)


def make_worker(out_dir: Path, **overrides) -> tuple[Worker, "queue.Queue"]:
    options = {
        "source": str(SOURCE),
        "out_dir": str(out_dir),
        "prefix": "시험",
        "recursive": False,
        "dedupe": True,
        "make_csv": True,
        "make_findings": True,
        "make_excel": True,
    }
    options.update(overrides)
    outbox: "queue.Queue" = queue.Queue()
    return Worker(options, outbox), outbox


def drain(outbox: "queue.Queue") -> dict[str, list]:
    messages: dict[str, list] = {}
    while not outbox.empty():
        message = outbox.get()
        messages.setdefault(message.kind, []).append(message)
    return messages


def test_produces_all_selected_outputs(tmp_path) -> None:
    worker, outbox = make_worker(tmp_path)
    worker._work()

    messages = drain(outbox)
    assert "error" not in messages, messages.get("error")
    done = messages["done"][0]
    names = {p.name for p in done.outputs}

    assert "시험.csv" in names
    assert "시험_위반내역.csv" in names
    assert "시험_분석.xlsx" in names
    assert all(p.exists() for p in done.outputs)


def test_only_selected_kinds_are_written(tmp_path) -> None:
    worker, outbox = make_worker(tmp_path, make_csv=False, make_excel=False)
    worker._work()

    done = drain(outbox)["done"][0]
    kinds = {p.suffix for p in done.outputs}
    assert ".xlsx" not in kinds
    assert any(p.name == "시험_위반내역.csv" for p in done.outputs)


def test_progress_is_reported_for_every_file(tmp_path) -> None:
    """진행 표시가 멈춰 보이면 사용자는 프로그램이 죽은 줄 안다."""
    worker, outbox = make_worker(tmp_path)
    worker._work()

    progress = drain(outbox)["progress"]
    assert progress[0].done == 0
    assert progress[-1].done == progress[-1].total > 0


def test_cancel_stops_before_writing(tmp_path) -> None:
    worker, outbox = make_worker(tmp_path)
    worker.stop_flag.set()          # 시작하자마자 중지
    worker._work()

    messages = drain(outbox)
    assert messages["done"][0].text == "중지됨"
    assert not messages["done"][0].outputs
    assert not list(tmp_path.iterdir()), "중지했는데 산출물이 생겼습니다"


def test_missing_path_is_reported_as_error(tmp_path) -> None:
    worker, outbox = make_worker(tmp_path, source=str(tmp_path / "없는폴더"))
    worker._work()

    messages = drain(outbox)
    assert "error" in messages
    assert "done" not in messages


def test_folder_without_inputs_is_reported_as_error(tmp_path) -> None:
    empty = tmp_path / "빈폴더"
    empty.mkdir()
    worker, outbox = make_worker(tmp_path, source=str(empty))
    worker._work()

    assert "error" in drain(outbox)


def test_unexpected_failure_does_not_hang_the_window(tmp_path, monkeypatch) -> None:
    """조용히 멈추는 것이 가장 나쁘다 — 예외는 반드시 화면까지 전달되어야 한다."""
    worker, outbox = make_worker(tmp_path)
    monkeypatch.setattr(worker, "_work", lambda: 1 / 0)
    worker.run()                    # 스레드 본체를 직접 호출

    messages = drain(outbox)
    assert "error" in messages
    assert "ZeroDivisionError" in messages["error"][0].text


# --------------------------------------------------------------------------
# 창 아이콘
# --------------------------------------------------------------------------


def test_missing_icon_does_not_break_startup(monkeypatch, tmp_path) -> None:
    """아이콘이 없다고 프로그램이 안 뜨면 안 된다."""
    from src import gui

    monkeypatch.setattr(gui, "ICON_ICO", tmp_path / "없음.ico")
    monkeypatch.setattr(gui, "ICON_PNG", tmp_path / "없음.png")

    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면이 없는 환경입니다")
    try:
        assert gui.apply_icon(root) == ""
    finally:
        root.destroy()


def test_broken_icon_is_ignored(monkeypatch, tmp_path) -> None:
    from src import gui

    broken = tmp_path / "icon.png"
    broken.write_bytes("이건 PNG 가 아니다".encode("utf-8"))
    monkeypatch.setattr(gui, "ICON_ICO", tmp_path / "없음.ico")
    monkeypatch.setattr(gui, "ICON_PNG", broken)

    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면이 없는 환경입니다")
    try:
        assert gui.apply_icon(root) == ""      # 예외 없이 조용히 넘어가야 한다
    finally:
        root.destroy()
