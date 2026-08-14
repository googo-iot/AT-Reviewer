"""장비 자동 판별.

CSV 하나를 받아서 어느 장비 프로파일에 해당하는지 고른다.

판별 결과가 애매하면 추측하지 않고 실패한다.
감사추적을 엉뚱한 프로파일로 읽으면 컬럼이 밀려서 조용히 틀린 결론이 나오는데,
그건 '못 읽었다'보다 훨씬 나쁘다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Iterator, Sequence

from .profile import (
    ACCESS_SUFFIXES,
    SQLITE_SUFFIXES,
    ParseResult,
    Profile,
    ProfileError,
    decode_file,
    load_profiles,
)

__all__ = [
    "AmbiguousProfileError",
    "DetectionError",
    "MatchReport",
    "NoProfileError",
    "Registry",
    "find_input_files",
]

#: 폴더를 훑을 때 입력으로 인정할 확장자.
INPUT_SUFFIXES: Final[tuple[str, ...]] = (
    ".csv", ".tsv", ".txt", ".pdf", *SQLITE_SUFFIXES, *ACCESS_SUFFIXES,
)

#: 글자로 훑을 수 없는 형식. 판별을 테이블 이름으로 한다.
DATABASE_SUFFIXES: Final[tuple[str, ...]] = (*SQLITE_SUFFIXES, *ACCESS_SUFFIXES)


class DetectionError(Exception):
    """장비를 특정하지 못했을 때의 공통 상위 예외."""


class NoProfileError(DetectionError):
    """어느 프로파일에도 걸리지 않음 — 프로파일을 새로 만들어야 하는 상황."""


class AmbiguousProfileError(DetectionError):
    """둘 이상 걸림 — detect 조건이 서로 겹쳐서 좁혀야 하는 상황."""


@dataclass(slots=True)
class MatchReport:
    """프로파일 하나가 파일과 얼마나 맞았는지. 실패 원인을 사람에게 설명하기 위한 것."""

    profile: Profile
    matched: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def is_match(self) -> bool:
        return not self.missing

    @property
    def score(self) -> int:
        return len(self.matched)

    def describe(self) -> str:
        if self.is_match:
            return f"{self.profile.id}: 전부 일치 ({', '.join(self.matched)})"
        found = ", ".join(self.matched) or "없음"
        return (
            f"{self.profile.id}: 일치 [{found}] / 누락 [{', '.join(self.missing)}]"
        )


class Registry:
    """프로파일 모음. 파일 → 프로파일 판별을 담당한다."""

    def __init__(self, profiles: Sequence[Profile]) -> None:
        if not profiles:
            raise ProfileError("프로파일이 하나도 없습니다.")
        self.profiles: list[Profile] = list(profiles)
        # 같은 파일을 프로파일 수만큼 다시 읽지 않도록 (경로, 지정인코딩) 캐시.
        # 판별에 쓴 본문을 파싱에서도 재사용한다 — PDF 는 추출이 가장 비싸다.
        self._text_cache: dict[tuple[Path, str | None], tuple[str, str]] = {}

    @classmethod
    def from_directory(cls, directory: Path | str = "config/profiles") -> Registry:
        return cls(load_profiles(directory))

    def __len__(self) -> int:
        return len(self.profiles)

    def __iter__(self) -> Iterator[Profile]:
        return iter(self.profiles)

    # -- 판별 --------------------------------------------------------------

    def _read(self, path: Path, profile: Profile) -> tuple[str, str]:
        key = (path, profile.encoding)
        prepared = self._text_cache.get(key)
        if prepared is None:
            prepared = decode_file(path, profile.encoding)
            self._text_cache[key] = prepared
        return prepared

    def _header_text(self, path: Path, profile: Profile) -> str:
        text, _ = self._read(path, profile)
        return "\n".join(text.splitlines()[: profile.search_lines])

    def inspect(self, path: Path | str) -> list[MatchReport]:
        """모든 프로파일에 대해 일치 상황을 계산한다. 점수 높은 순으로 정렬."""
        target = Path(path)
        is_database = target.suffix.lower() in DATABASE_SUFFIXES

        reports: list[MatchReport] = []
        for profile in self.profiles:
            if profile.read_format in {"sqlite", "access"}:
                # DB 는 본문을 글자로 훑을 수 없다. 테이블 이름으로 판별한다.
                # 비밀번호가 필요한 형식이 있어 프로파일별로 물어본다.
                tables = {n.lower() for n in profile.available_tables(target)}
                wanted = profile.detect_tables
                matched = tuple(t for t in wanted if t.lower() in tables)
                missing = tuple(t for t in wanted if t.lower() not in tables)
            elif is_database:
                # 글자 기반 프로파일에 DB 를 들이대면 디코딩만 실패한다. 아예 건너뛴다.
                matched, missing = (), profile.detect_tokens
            else:
                header = self._header_text(target, profile).lower()
                matched = tuple(t for t in profile.header_contains if t.lower() in header)
                missing = tuple(
                    t for t in profile.header_contains if t.lower() not in header
                )
            reports.append(MatchReport(profile=profile, matched=matched, missing=missing))
        reports.sort(key=lambda r: (r.is_match, r.score), reverse=True)
        return reports

    def identify(self, path: Path | str) -> Profile:
        """파일에 맞는 프로파일 하나를 고른다. 0개거나 2개 이상이면 예외."""
        target = Path(path)
        reports = self.inspect(target)
        hits = [r for r in reports if r.is_match]

        if len(hits) == 1:
            return hits[0].profile

        if not hits:
            raise NoProfileError(self._describe_no_match(target, reports))
        raise AmbiguousProfileError(self._describe_ambiguous(target, hits))

    def _describe_no_match(self, path: Path, reports: list[MatchReport]) -> str:
        lines = [
            f"'{path.name}' 에 맞는 장비 프로파일이 없습니다. "
            f"({len(self.profiles)}개 검사)",
            "  검사 결과 (일치가 많은 순):",
        ]
        for report in reports[:5]:
            lines.append(f"    - {report.describe()}")
        first_line = self._first_nonempty_line(path)
        if first_line:
            lines.append(f"  파일 첫 줄: {first_line[:160]}")
        lines.append(
            "  → config/profiles/ 에 이 장비용 YAML 을 추가하거나, "
            "기존 프로파일의 detect.header_contains 를 조정하세요."
        )
        return "\n".join(lines)

    def _describe_ambiguous(self, path: Path, hits: list[MatchReport]) -> str:
        lines = [
            f"'{path.name}' 이(가) 프로파일 {len(hits)}개에 동시에 걸렸습니다. "
            f"어느 장비인지 확정할 수 없습니다.",
        ]
        for report in hits:
            source = report.profile.source.name if report.profile.source else "?"
            lines.append(
                f"    - {report.profile.id} ({source}): "
                f"판별조건 = {list(report.profile.detect_tokens)}"
            )
        lines.append(
            "  → 각 프로파일의 detect 조건에 "
            "해당 장비에만 있는 컬럼명/테이블명을 추가해 구분하세요."
        )
        return "\n".join(lines)

    def _first_nonempty_line(self, path: Path) -> str:
        """진단 메시지용. 여기서 디코딩까지 실패하면 조용히 포기한다."""
        try:
            text, _ = decode_file(path, None)
        except ProfileError:
            return ""
        for line in text.splitlines():
            if line.strip():
                return line.strip()
        return ""

    # -- 판별 + 파싱 -------------------------------------------------------

    def read(
        self, path: Path | str, equipment_id: str | None = None
    ) -> tuple[Profile, ParseResult]:
        """판별부터 AuditEvent 변환까지 한 번에. 파일은 한 번만 읽는다."""
        target = Path(path)
        profile = self.identify(target)
        if profile.read_format in {"sqlite", "access"}:   # 미리 읽어둘 본문이 없다
            return profile, profile.parse_file(target, equipment_id=equipment_id)
        return profile, profile.parse_file(
            target, self._read(target, profile), equipment_id=equipment_id
        )

    def parse_file(self, path: Path | str) -> ParseResult:
        return self.read(path)[1]

    def forget(self, path: Path | str) -> None:
        """캐시에서 한 파일을 비운다. 파일이 많을 때 메모리를 붙들지 않기 위해."""
        target = Path(path)
        for key in [k for k in self._text_cache if k[0] == target]:
            del self._text_cache[key]


# --------------------------------------------------------------------------
# 입력 수집
# --------------------------------------------------------------------------


def find_input_files(
    path: Path | str,
    suffixes: Iterable[str] = INPUT_SUFFIXES,
    *,
    recursive: bool = False,
) -> list[Path]:
    """파일이면 그 파일 하나, 폴더면 그 폴더 안의 파일들.

    기본은 지정한 폴더만 본다. 하위 폴더는 보통 다른 장비나 다른 목적의
    묶음이라, 폴더를 지정했을 때 딸려 들어오면 산출물이 조용히 오염된다.
    하위까지 포함하려면 recursive=True.

    숨김 파일과 Excel 임시파일(~$...)은 제외한다.
    """
    root = Path(path)
    allowed = {s.lower() for s in suffixes}

    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"경로를 찾을 수 없습니다: {root}")

    candidates = root.rglob("*") if recursive else root.glob("*")
    return [
        p
        for p in sorted(candidates)
        if p.is_file()
        and p.suffix.lower() in allowed
        and not p.name.startswith((".", "~$"))
    ]


def count_nested_inputs(
    path: Path | str, suffixes: Iterable[str] = INPUT_SUFFIXES
) -> int:
    """하위 폴더에만 있는 입력 파일 수. '이만큼 빼고 처리했다'고 알리기 위한 것."""
    root = Path(path)
    if not root.is_dir():
        return 0
    shallow = {p.resolve() for p in find_input_files(root, suffixes)}
    deep = {p.resolve() for p in find_input_files(root, suffixes, recursive=True)}
    return len(deep - shallow)
