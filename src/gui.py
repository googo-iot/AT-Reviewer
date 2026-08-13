"""AT-Reviewer 창 프로그램.

    python -m src.gui          (또는 AT-Reviewer.bat 더블클릭)

처리 절차는 core/pipeline.py 에 있다. 이 파일이 하는 일은
'고르게 하고, 진행을 보여주고, 결과를 열어주는 것'뿐이다.

PDF 추출은 오래 걸리므로 처리는 별도 스레드에서 돌린다.
화면은 큐를 통해서만 갱신한다 — tkinter 는 다른 스레드에서 건드리면 안 된다.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, PhotoImage, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from .core.excel import ExcelError, write_workbook
from .core.export import write_failures_csv, write_findings_csv, write_profile_csv
from .core.pipeline import FileReport, run
from .core.profile import ProfileError
from .core.registry import Registry, count_nested_inputs, find_input_files
from .core.rules import RuleError, evaluate, load_rules
from .cli import build_sheets

APP_TITLE = "AT-Reviewer — 감사추적 분석"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "config" / "local_gui.json"   # config/local* 은 gitignore 대상
ASSETS = PROJECT_ROOT / "assets"

#: 창 아이콘. .ico 가 있으면 그것을, 없으면 .png 를 쓴다.
#: 둘 다 없어도 프로그램은 그대로 돌아간다 — 아이콘 때문에 실행이 막히면 안 된다.
ICON_ICO = ASSETS / "icon.ico"
ICON_PNG = ASSETS / "icon.png"

DISCLAIMER = (
    "검증되지 않은 개인 분석 도구입니다. 최종 판정은 원본 감사추적 확인이 필요합니다."
)


# --------------------------------------------------------------------------
# 작업 스레드 ↔ 화면 사이의 메시지
# --------------------------------------------------------------------------


@dataclass
class Message:
    kind: str          # log | progress | done | error
    text: str = ""
    done: int = 0
    total: int = 0
    outputs: tuple[Path, ...] = ()


class Worker(threading.Thread):
    """실제 처리. 화면을 직접 건드리지 않고 큐에만 넣는다."""

    def __init__(self, options: dict, outbox: "queue.Queue[Message]") -> None:
        super().__init__(daemon=True)
        self.options = options
        self.outbox = outbox
        self.stop_flag = threading.Event()

    # -- 편의 ---------------------------------------------------------------

    def log(self, text: str = "") -> None:
        self.outbox.put(Message("log", text))

    def progress(self, done: int, total: int) -> None:
        self.outbox.put(Message("progress", done=done, total=total))

    # -- 본체 ---------------------------------------------------------------

    def run(self) -> None:
        try:
            self._work()
        except Exception:                       # 창이 조용히 멈추는 것이 가장 나쁘다
            self.outbox.put(Message("error", traceback.format_exc()))

    def _work(self) -> None:
        options = self.options
        source = Path(options["source"])
        out_dir = Path(options["out_dir"])
        prefix = options["prefix"] or "감사추적"

        # -- 설정 로드 ------------------------------------------------------
        try:
            registry = Registry.from_directory(PROJECT_ROOT / "config" / "profiles")
            specs = load_rules(PROJECT_ROOT / "config" / "rules" / "default.yaml")
        except (ProfileError, RuleError) as exc:
            self.outbox.put(Message("error", f"설정을 불러오지 못했습니다.\n\n{exc}"))
            return

        try:
            files = find_input_files(source, recursive=options["recursive"])
        except FileNotFoundError as exc:
            self.outbox.put(Message("error", str(exc)))
            return
        if not files:
            self.outbox.put(Message("error", f"처리할 파일이 없습니다.\n\n{source}"))
            return

        self.log(f"프로파일 {len(registry)}개 / 규칙 {len(specs)}개 / 입력 {len(files)}개")
        if not options["recursive"]:
            nested = count_nested_inputs(source)
            if nested:
                self.log(f"  (하위 폴더의 {nested}개는 제외했습니다)")
        self.log()

        # -- 읽기 -----------------------------------------------------------
        done = 0
        self.progress(0, len(files))

        def on_file(report: FileReport) -> None:
            nonlocal done
            done += 1
            if report.status == "detect_failed":
                self.log(f"  [판별실패] {report.name}")
            elif report.status == "read_failed":
                self.log(f"  [읽기실패] {report.name}")
            else:
                flag = f"   ← {report.errors}건 실패" if report.errors else ""
                self.log(f"  {report.name:<22} {report.events:>6,}건{flag}")
            self.progress(done, len(files))

        result = run(
            files,
            registry,
            dedupe=options["dedupe"],
            on_file=on_file,
            cancelled=self.stop_flag.is_set,
        )

        if result.cancelled:
            self.log("\n사용자가 중지했습니다. 산출물은 만들지 않았습니다.")
            self.outbox.put(Message("done", "중지됨"))
            return
        if not result.total_events:
            self.outbox.put(Message("error", "변환된 이벤트가 없습니다."))
            return

        events = result.events
        findings = evaluate(events, specs)

        # -- 쓰기 -----------------------------------------------------------
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        many = len(result.per_equipment) > 1

        try:
            if options["make_csv"]:
                for equipment_id, group in result.per_equipment.items():
                    name = f"{prefix}_{equipment_id}.csv" if many else f"{prefix}.csv"
                    target = out_dir / name
                    write_profile_csv(result.profiles[equipment_id], group, target)
                    outputs.append(target)
                    self.log(f"\n  공통표    {len(group):,}건  →  {target.name}")

                if result.removed:
                    target = out_dir / f"{prefix}_중복.csv"
                    first = next(iter(result.per_equipment))
                    write_profile_csv(result.profiles[first], result.removed, target)
                    outputs.append(target)
                    self.log(f"  중복제외  {len(result.removed):,}건  →  {target.name}")

            if options["make_findings"]:
                target = out_dir / f"{prefix}_위반내역.csv"
                write_findings_csv(findings, target)
                outputs.append(target)
                self.log(f"  위반내역  {len(findings):,}건  →  {target.name}")

            if options["make_excel"]:
                target = out_dir / f"{prefix}_분석.xlsx"
                sheets = build_sheets(
                    result, findings, specs, str(source), options["recursive"]
                )
                write_workbook(sheets, target)
                outputs.append(target)
                self.log(f"  Excel     {len(sheets)}개 시트  →  {target.name}")

            if result.failures:
                target = out_dir / f"{prefix}_실패.csv"
                write_failures_csv(result.failures, target)
                outputs.append(target)
                self.log(f"  실패내역  {len(result.failures):,}건  →  {target.name}")

        except PermissionError as exc:
            self.outbox.put(Message(
                "error",
                "산출물에 쓸 수 없습니다.\n\n"
                f"{getattr(exc, 'filename', '') or ''}\n\n"
                "해당 파일이 Excel 등에서 열려 있으면 닫고 다시 실행하세요.",
            ))
            return
        except (ExcelError, OSError) as exc:
            self.outbox.put(Message("error", f"산출물을 쓰지 못했습니다.\n\n{exc}"))
            return

        self._log_summary(result, findings, specs)
        self.outbox.put(Message("done", "완료", outputs=tuple(outputs)))

    # -- 요약 ---------------------------------------------------------------

    def _log_summary(self, result, findings, specs) -> None:
        events = result.events
        line = "─" * 52
        self.log(f"\n{line}")
        self.log(f"파일      {result.ok_files}/{result.total_files} 처리"
                 + (f"  (실패 {result.failed_files})" if result.failed_files else ""))
        self.log(f"이벤트    {len(events):,}건"
                 + (f"  (레코드 실패 {result.row_failures:,})" if result.row_failures else ""))
        if result.skipped:
            self.log(f"무시      {result.skipped:,}건 (페이지 머리글 등)")
        if result.removed:
            self.log(f"중복제거  {len(result.removed):,}건")
            for text in result.overlaps:
                self.log(f"          {text}")
        if events:
            self.log(f"기간      {min(e.timestamp for e in events):%Y-%m-%d}"
                     f" ~ {max(e.timestamp for e in events):%Y-%m-%d}")
            self.log(f"행위자    {len({e.actor for e in events})}명")

        self.log(f"\n위반 의심 {len(findings):,}건")
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
        for spec in sorted(specs, key=lambda s: (s.severity, s.name)):
            count = counts.get(spec.name, 0)
            mark = "■" if count else "·"
            self.log(f"  {mark} {spec.description:<20} {count:>5,}건")

        high = [f for f in findings if f.severity == "high"]
        if high:
            self.log("\n  [높음] 상위 건")
            for finding in high[:6]:
                self.log(f"    {finding.event.timestamp:%Y-%m-%d %H:%M}  "
                         f"{finding.event.actor}")
                self.log(f"        {finding.evidence}")
            if len(high) > 6:
                self.log(f"    … 외 {len(high) - 6:,}건")
        self.log(line)


# --------------------------------------------------------------------------
# 창
# --------------------------------------------------------------------------


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.outbox: "queue.Queue[Message]" = queue.Queue()
        self.worker: Worker | None = None
        self.outputs: tuple[Path, ...] = ()

        root.title(APP_TITLE)
        root.minsize(760, 620)

        self.source = StringVar()
        self.out_dir = StringVar(value=str(PROJECT_ROOT / "output"))
        self.prefix = StringVar(value="감사추적")
        self.recursive = BooleanVar(value=False)
        self.dedupe = BooleanVar(value=True)
        self.make_csv = BooleanVar(value=True)
        self.make_findings = BooleanVar(value=True)
        self.make_excel = BooleanVar(value=True)
        self.status = StringVar(value="분석할 폴더를 고르세요.")

        self._build()
        self._load_settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    # -- 화면 구성 -----------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frame, text="1. 분석할 대상", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        row += 1
        ttk.Label(frame, text="폴더 / 파일").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.source).grid(row=row, column=1, sticky="ew", **pad)
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=2, sticky="w")
        ttk.Button(buttons, text="폴더…", width=7, command=self._pick_folder).pack(side="left", padx=2)
        ttk.Button(buttons, text="파일…", width=7, command=self._pick_file).pack(side="left", padx=2)

        row += 1
        options = ttk.Frame(frame)
        options.grid(row=row, column=1, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(options, text="하위 폴더까지 포함", variable=self.recursive).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(options, text="파일 간 중복 제거", variable=self.dedupe).pack(side="left")

        row += 1
        ttk.Separator(frame).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)

        row += 1
        ttk.Label(frame, text="2. 만들 산출물", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        row += 1
        kinds = ttk.Frame(frame)
        kinds.grid(row=row, column=1, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(kinds, text="공통표 CSV", variable=self.make_csv).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(kinds, text="위반내역 CSV", variable=self.make_findings).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(kinds, text="Excel (3시트)", variable=self.make_excel).pack(side="left")

        row += 1
        ttk.Label(frame, text="저장 폴더").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.out_dir).grid(row=row, column=1, sticky="ew", **pad)
        ttk.Button(frame, text="폴더…", width=7, command=self._pick_out).grid(row=row, column=2, sticky="w", padx=2)

        row += 1
        ttk.Label(frame, text="파일 이름 앞머리").grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.prefix, width=24).grid(row=row, column=1, sticky="w", **pad)

        row += 1
        ttk.Separator(frame).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)

        row += 1
        action = ttk.Frame(frame)
        action.grid(row=row, column=0, columnspan=3, sticky="ew")
        action.columnconfigure(2, weight=1)
        self.run_button = ttk.Button(action, text="실행", width=12, command=self._start)
        self.run_button.grid(row=0, column=0, padx=(8, 4))
        self.stop_button = ttk.Button(action, text="중지", width=8, command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=4)
        self.bar = ttk.Progressbar(action, mode="determinate")
        self.bar.grid(row=0, column=2, sticky="ew", padx=8)
        self.count = ttk.Label(action, text="", width=10)
        self.count.grid(row=0, column=3, padx=(0, 8))

        row += 1
        ttk.Label(frame, textvariable=self.status, foreground="#444").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2)
        )

        row += 1
        frame.rowconfigure(row, weight=1)
        self.log = ScrolledText(frame, height=18, font=("Consolas", 9), wrap="none")
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        self.log.configure(state="disabled")

        row += 1
        bottom = ttk.Frame(frame)
        bottom.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.open_button = ttk.Button(
            bottom, text="산출물 폴더 열기", command=self._open_out, state="disabled"
        )
        self.open_button.pack(side="left", padx=8)
        ttk.Label(bottom, text=f"※ {DISCLAIMER}", foreground="#a33").pack(side="left", padx=8)

    # -- 파일 고르기 ---------------------------------------------------------

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(title="분석할 폴더", initialdir=self._start_dir())
        if chosen:
            self.source.set(chosen)

    def _pick_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="분석할 파일",
            initialdir=self._start_dir(),
            filetypes=[("감사추적 파일", "*.pdf *.csv *.tsv *.txt"), ("모든 파일", "*.*")],
        )
        if chosen:
            self.source.set(chosen)

    def _pick_out(self) -> None:
        chosen = filedialog.askdirectory(title="저장 폴더", initialdir=self.out_dir.get() or ".")
        if chosen:
            self.out_dir.set(chosen)

    def _start_dir(self) -> str:
        current = self.source.get()
        if current and Path(current).exists():
            return str(Path(current) if Path(current).is_dir() else Path(current).parent)
        data = PROJECT_ROOT / "data"
        return str(data if data.is_dir() else PROJECT_ROOT)

    # -- 실행 ---------------------------------------------------------------

    def _start(self) -> None:
        source = self.source.get().strip()
        if not source:
            messagebox.showwarning(APP_TITLE, "분석할 폴더나 파일을 먼저 고르세요.")
            return
        if not Path(source).exists():
            messagebox.showerror(APP_TITLE, f"경로를 찾을 수 없습니다.\n\n{source}")
            return
        if not (self.make_csv.get() or self.make_findings.get() or self.make_excel.get()):
            messagebox.showwarning(APP_TITLE, "만들 산출물을 하나 이상 고르세요.")
            return

        self._clear_log()
        self._append(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 시작\n")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.bar.configure(value=0, maximum=100)
        self.status.set("처리 중입니다. PDF 는 한 개당 몇 초씩 걸릴 수 있습니다.")

        self.worker = Worker(
            {
                "source": source,
                "out_dir": self.out_dir.get().strip() or str(PROJECT_ROOT / "output"),
                "prefix": self.prefix.get().strip(),
                "recursive": self.recursive.get(),
                "dedupe": self.dedupe.get(),
                "make_csv": self.make_csv.get(),
                "make_findings": self.make_findings.get(),
                "make_excel": self.make_excel.get(),
            },
            self.outbox,
        )
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None:
            self.worker.stop_flag.set()
            self.status.set("중지하는 중입니다. 처리 중인 파일이 끝나면 멈춥니다.")
            self.stop_button.configure(state="disabled")

    # -- 큐 처리 (화면 갱신은 여기서만) --------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                message = self.outbox.get_nowait()
                self._handle(message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, message: Message) -> None:
        if message.kind == "log":
            self._append(message.text)
        elif message.kind == "progress":
            self.bar.configure(maximum=max(message.total, 1), value=message.done)
            self.count.configure(text=f"{message.done}/{message.total}")
        elif message.kind == "done":
            self.outputs = message.outputs
            self._finish(message.text)
        elif message.kind == "error":
            self._append(f"\n[오류]\n{message.text}")
            self._finish("오류")
            messagebox.showerror(APP_TITLE, message.text)

    def _finish(self, label: str) -> None:
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.worker = None
        if self.outputs:
            self.open_button.configure(state="normal")
            self.status.set(f"{label} — 산출물 {len(self.outputs)}개를 만들었습니다.")
            self._save_settings()
        else:
            self.status.set(label)

    # -- 로그 ---------------------------------------------------------------

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # -- 산출물 열기 ---------------------------------------------------------

    def _open_out(self) -> None:
        target = Path(self.out_dir.get())
        try:
            if sys.platform == "win32":
                os.startfile(target)                        # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"폴더를 열지 못했습니다.\n\n{exc}")

    # -- 설정 기억 -----------------------------------------------------------

    def _save_settings(self) -> None:
        data = {
            "source": self.source.get(),
            "out_dir": self.out_dir.get(),
            "prefix": self.prefix.get(),
            "recursive": self.recursive.get(),
            "dedupe": self.dedupe.get(),
            "make_csv": self.make_csv.get(),
            "make_findings": self.make_findings.get(),
            "make_excel": self.make_excel.get(),
        }
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass          # 설정 저장 실패로 작업을 막지는 않는다

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, var in (
            ("source", self.source), ("out_dir", self.out_dir), ("prefix", self.prefix),
            ("recursive", self.recursive), ("dedupe", self.dedupe),
            ("make_csv", self.make_csv), ("make_findings", self.make_findings),
            ("make_excel", self.make_excel),
        ):
            if key in data:
                var.set(data[key])

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel(APP_TITLE, "처리 중입니다. 정말 닫을까요?"):
                return
            self.worker.stop_flag.set()
        self.root.destroy()


def _set_windows_icons(root: Tk, path: Path) -> bool:
    """Windows 에 큰 아이콘·작은 아이콘을 크기별로 직접 지정한다.

    tkinter 의 iconbitmap 만 쓰면 Windows 가 16px 짜리를 집어
    작업표시줄 크기(24px)로 '늘려서' 그린다. 확대라서 무조건 뭉개진다.
    LoadImage 로 원하는 크기를 정확히 꺼내 WM_SETICON 으로 붙이면
    작업표시줄이 큰 아이콘(32px)을 줄여 쓰게 되어 훨씬 또렷하다.
    """
    import ctypes

    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x0010, 0x0080
    ICON_SMALL, ICON_BIG = 0, 1

    user32 = ctypes.windll.user32
    root.update_idletasks()                   # 창이 실제로 만들어져야 핸들이 생긴다
    try:
        hwnd = int(root.wm_frame(), 16)
    except (ValueError, Exception):
        return False

    ok = False
    for size, which in ((16, ICON_SMALL), (32, ICON_BIG)):
        handle = user32.LoadImageW(
            None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE
        )
        if handle:
            user32.SendMessageW(hwnd, WM_SETICON, which, handle)
            root._icon_handles = getattr(root, "_icon_handles", []) + [handle]
            ok = True
    return ok


def apply_icon(root: Tk) -> str:
    """창 아이콘을 붙인다. 무엇을 썼는지 돌려준다 ('' 이면 못 붙인 것).

    아이콘이 없거나 형식이 안 맞아도 프로그램은 그대로 떠야 한다.
    """
    if sys.platform == "win32" and ICON_ICO.is_file():
        try:
            root.iconbitmap(default=str(ICON_ICO))     # 창 왼쪽 위 / 대화상자용
            _set_windows_icons(root, ICON_ICO)         # 작업표시줄용
            return ICON_ICO.name
        except Exception:
            pass                              # .ico 가 깨졌으면 png 로 넘어간다

    if ICON_PNG.is_file():
        try:
            image = PhotoImage(file=str(ICON_PNG))
            # 원본이 크면 창 아이콘으로 쓰기에 무겁다. 64px 근처로 줄인다.
            factor = max(1, image.width() // 64)
            if factor > 1:
                image = image.subsample(factor, factor)
            root.iconphoto(True, image)
            root._icon_ref = image            # 참조를 놓으면 아이콘이 사라진다
            return ICON_PNG.name
        except Exception:
            pass
    return ""


def _claim_taskbar_identity() -> None:
    """작업표시줄에서 파이썬이 아니라 이 프로그램으로 묶이게 한다.

    이걸 안 하면 Windows 가 pythonw.exe 로 묶어 파이썬 아이콘을 쓸 수 있다.
    창을 만들기 전에 불러야 한다.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AT-Reviewer.GUI")
    except Exception:
        pass


def main() -> int:
    _claim_taskbar_identity()
    root = Tk()
    try:
        ttk.Style().theme_use("vista")     # Windows 기본 테마가 더 보기 좋다
    except Exception:
        pass
    apply_icon(root)
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
