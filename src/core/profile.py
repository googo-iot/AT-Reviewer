"""장비 프로파일 로더.

config/profiles/*.yaml 한 장이 장비 하나를 설명한다.
새 장비를 붙일 때 Python 코드를 건드리지 않는 것이 이 모듈의 목적이다.

입력 형식은 두 가지를 지원한다.

1) delimited — 구분자 기반 표 (CSV/TSV)

    read:
      format: delimited        # 생략 시 기본값
      encoding: cp949
      delimiter: ","
      skip_rows: 0

2) blocks — 구분선으로 나뉜 '키: 값' 블록 리포트 (PDF 내보내기 등)

    read:
      format: blocks
      block_separator: "[-–—_]{20,}"   # 정규식
      fields: ["Type", "Action", "Date"]         # 키로 인정할 이름
      require_fields: ["Date", "Action"]         # 없으면 그 블록은 버린다
      skip_if_fields: ["Page"]                   # 있으면 그 블록은 버린다

출력 표의 컬럼은 장비마다 다르므로 프로파일이 직접 정한다.

    output:
      columns:
        - {name: "발생일시", from: timestamp, format: "%Y-%m-%d %H:%M:%S"}
        - {name: "유형",     from: "Type"}       # 원본 컬럼/필드 이름
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

import yaml

from .schema import ACTIONS, AuditEvent, SchemaError

__all__ = [
    "ENCODING_FALLBACKS",
    "OutputColumn",
    "ParseResult",
    "Profile",
    "ProfileError",
    "RowError",
    "extract_text",
    "load_profiles",
    "sniff_header",
]


# --------------------------------------------------------------------------
# 상수
# --------------------------------------------------------------------------

#: 지정 인코딩이 실패했을 때 순서대로 재시도할 후보.
ENCODING_FALLBACKS: Final[tuple[str, ...]] = ("utf-8-sig", "cp949", "utf-16")

#: AuditEvent 생성에 반드시 있어야 하는 매핑.
REQUIRED_FIELDS: Final[tuple[str, ...]] = ("timestamp", "actor", "action")

#: 있으면 쓰고 없으면 None으로 두는 매핑.
OPTIONAL_FIELDS: Final[tuple[str, ...]] = ("target", "old_value", "new_value", "reason")

#: map 에 쓸 수 있는 전체 필드.
MAPPABLE_FIELDS: Final[tuple[str, ...]] = REQUIRED_FIELDS + OPTIONAL_FIELDS

#: output.columns 의 from 에 쓸 수 있는 공통 스키마 이름.
#: 여기에 없는 이름은 '원본 필드 이름'으로 해석한다.
SCHEMA_SOURCES: Final[frozenset[str]] = frozenset(
    {*MAPPABLE_FIELDS, "equipment_id", "source_file", "source_name", "line"}
)

#: timestamp 에 format 을 지정하지 않았을 때 순서대로 시도할 형식.
DEFAULT_TIME_FORMATS: Final[tuple[str, ...]] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d-%b-%y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%d.%b.%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)

#: 출력 시각 기본 형식.
DEFAULT_OUTPUT_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

#: detect 시 훑어볼 파일 앞부분 줄 수.
DEFAULT_SEARCH_LINES: Final[int] = 10

#: blocks 형식의 기본 구분선 — 하이픈/en·em 대시/밑줄이 20자 이상 이어지는 줄.
DEFAULT_BLOCK_SEPARATOR: Final[str] = r"[-–—_=]{20,}"

_DELIMITER_ALIASES: Final[dict[str, str]] = {
    "tab": "\t",
    "\\t": "\t",
    "comma": ",",
    "semicolon": ";",
    "pipe": "|",
}


class ProfileError(Exception):
    """프로파일 정의 자체가 잘못됐을 때 (YAML 오류, 필수 항목 누락 등)."""


@dataclass(slots=True)
class RowError:
    """한 레코드를 변환하지 못했을 때의 기록. 파일 전체를 버리지 않기 위해 존재한다."""

    line: int
    message: str
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.line}행: {self.message}"


@dataclass(slots=True)
class ParseResult:
    """한 파일을 한 프로파일로 읽은 결과."""

    path: Path
    profile_id: str
    encoding: str
    events: list[AuditEvent] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    skipped: int = 0          # 의도적으로 버린 블록 수 (페이지 머리글 등)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __str__(self) -> str:
        tail = f", {self.skipped}건 무시" if self.skipped else ""
        return (
            f"{self.path.name}: {len(self.events)}건 변환, "
            f"{len(self.errors)}건 실패{tail} "
            f"(프로파일={self.profile_id}, {self.encoding})"
        )


@dataclass(slots=True)
class OutputColumn:
    """출력 표의 컬럼 하나. 장비마다 다르므로 프로파일이 정의한다."""

    name: str                  # 산출물에 찍힐 컬럼명
    source: str                # 공통 스키마 이름 또는 원본 필드 이름
    time_format: str | None = None

    @property
    def is_schema(self) -> bool:
        return self.source in SCHEMA_SOURCES


# --------------------------------------------------------------------------
# 파일 읽기 (PDF / 인코딩 폴백)
# --------------------------------------------------------------------------


def _encoding_candidates(preferred: str | None) -> list[str]:
    candidates: list[str] = []
    for enc in (preferred, *ENCODING_FALLBACKS):
        if not enc:
            continue
        normalized = enc.strip().lower()
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _extract_pdf_text(path: Path) -> str:
    """PDF 에서 텍스트를 뽑는다. 페이지 사이는 줄바꿈으로만 잇는다.

    페이지 구분자를 따로 넣지 않는 이유: 블록이 페이지 경계에 걸쳐 있어도
    구분선 기준으로 잘리면 그대로 복원되기 때문이다.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise ProfileError(
            "PDF 를 읽으려면 pypdf 가 필요합니다: pip install pypdf"
        ) from exc

    try:
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except ProfileError:
        raise
    except Exception as exc:
        raise ProfileError(f"PDF 를 읽지 못했습니다: {path} ({exc})") from exc

    # PDF 는 칸 맞춤에 탭을 쓴다. 추출하면 'Signed\tby:' 처럼 낱말 사이에 탭이 남아
    # 키 이름이 안 맞는다. 여기서 공백으로 통일한다.
    # (탭을 구분자로 쓰는 TSV 는 이 경로를 타지 않으므로 영향이 없다.)
    return text.replace("\t", " ")


def extract_text(path: Path, preferred: str | None = None) -> tuple[str, str]:
    """파일 본문을 텍스트로 만든다. (본문, 사용한 인코딩/방식) 반환.

    확장자가 .pdf 면 텍스트 추출을 쓴다.
    파일 형식은 프로파일이 아니라 파일 자체가 결정하므로,
    어느 프로파일로 판별하기 전에도 읽을 수 있다.

    그 외에는 지정 인코딩 → utf-8-sig → cp949 → utf-16 순으로 시도한다.
    전부 실패하면 손실 허용 디코딩을 하지 않고 실패시킨다
    — 감사추적을 깨진 문자로 읽는 것보다 못 읽었다고 말하는 편이 낫다.
    """
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_text(path), "pdf"

    data = path.read_bytes()
    attempted: list[str] = []
    for encoding in _encoding_candidates(preferred):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            attempted.append(f"{encoding}({type(exc).__name__})")
            continue
        # utf-16 은 잘못된 파일도 cp949 로 '성공'하는 경우가 있어 NUL 혼입으로 거른다.
        if "\x00" in text:
            attempted.append(f"{encoding}(NUL 문자 검출)")
            continue
        return text, encoding
    raise ProfileError(
        f"인코딩을 판별하지 못했습니다: {path} — 시도: {', '.join(attempted)}"
    )


def sniff_header(
    path: Path,
    preferred: str | None = None,
    search_lines: int = DEFAULT_SEARCH_LINES,
) -> str:
    """장비 판별용으로 파일 앞부분 몇 줄을 텍스트로 돌려준다."""
    text, _ = extract_text(path, preferred)
    return "\n".join(text.splitlines()[:search_lines])


# 이전 이름 호환 (registry 등에서 사용)
decode_file = extract_text


# --------------------------------------------------------------------------
# 값 변환 헬퍼
# --------------------------------------------------------------------------


def _as_format_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_TIME_FORMATS
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(v) for v in value)
    raise ProfileError(f"timestamp format 은 문자열 또는 목록이어야 합니다: {value!r}")


def _parse_timestamp(text: str, formats: Iterable[str]) -> datetime:
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    raise SchemaError(
        f"시각을 해석하지 못했습니다: {text!r} (시도한 형식: {', '.join(formats)})"
    )


def _resolve_delimiter(value: Any) -> str:
    if value is None:
        return ","
    text = _DELIMITER_ALIASES.get(str(value).strip().lower(), str(value))
    if len(text) != 1:
        raise ProfileError(
            f'delimiter 는 한 글자여야 합니다: {value!r} (탭은 "\\t" 또는 tab)'
        )
    return text


def _as_str_list(value: Any, where: str, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(v) for v in value)
    raise ProfileError(f"{label} 은 문자열 또는 목록이어야 합니다{where}")


# --------------------------------------------------------------------------
# 프로파일
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Profile:
    """장비 하나의 파싱 규칙 + 출력 규칙. YAML 한 장에 1:1 대응."""

    id: str
    name: str
    header_contains: tuple[str, ...]
    read_format: str                     # delimited | blocks
    encoding: str | None
    mapping: dict[str, dict[str, Any]]
    vocabulary: dict[str, dict[str, str]]
    # delimited 전용
    delimiter: str = ","
    skip_rows: int = 0
    # blocks 전용
    block_separator: str = DEFAULT_BLOCK_SEPARATOR
    block_fields: tuple[str, ...] = ()
    require_fields: tuple[str, ...] = ()
    skip_if_fields: tuple[str, ...] = ()
    # 공통
    output_columns: tuple[OutputColumn, ...] = ()
    search_lines: int = DEFAULT_SEARCH_LINES
    source: Path | None = None

    # -- 로딩 --------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path) -> Profile:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ProfileError(f"YAML 문법 오류: {path}\n{exc}") from exc
        except OSError as exc:
            raise ProfileError(f"프로파일을 읽지 못했습니다: {path} ({exc})") from exc
        if not isinstance(raw, dict):
            raise ProfileError(f"프로파일 최상위는 매핑이어야 합니다: {path}")
        return cls.from_dict(raw, source=path)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: Path | None = None) -> Profile:
        where = f" ({source})" if source else ""

        if not raw.get("id"):
            raise ProfileError(f"'id' 항목이 필요합니다{where}")
        profile_id = str(raw["id"]).strip()
        name = str(raw.get("name") or profile_id).strip()

        # -- detect ---------------------------------------------------------
        detect = raw.get("detect") or {}
        if not isinstance(detect, dict):
            raise ProfileError(f"detect 는 매핑이어야 합니다{where}")
        header_contains = _as_str_list(
            detect.get("header_contains"), where, "detect.header_contains"
        )
        if not header_contains:
            raise ProfileError(
                f"detect.header_contains 가 비어 있습니다{where} — "
                f"판별 조건이 없으면 아무 파일에나 걸립니다."
            )
        search_lines = int(detect.get("search_lines", DEFAULT_SEARCH_LINES))

        # -- read -----------------------------------------------------------
        read = raw.get("read") or {}
        if not isinstance(read, dict):
            raise ProfileError(f"read 는 매핑이어야 합니다{where}")
        read_format = str(read.get("format", "delimited")).strip().lower()
        if read_format not in {"delimited", "blocks"}:
            raise ProfileError(
                f"read.format 은 delimited 또는 blocks 여야 합니다{where}: {read_format}"
            )

        block_fields = _as_str_list(read.get("fields"), where, "read.fields")
        if read_format == "blocks" and not block_fields:
            raise ProfileError(
                f"read.fields 가 필요합니다{where} — "
                f"블록에서 무엇을 키로 인정할지 알려주어야 합니다."
            )

        # -- map ------------------------------------------------------------
        mapping_raw = raw.get("map")
        if not isinstance(mapping_raw, dict) or not mapping_raw:
            raise ProfileError(f"map 이 필요합니다{where}")

        mapping: dict[str, dict[str, Any]] = {}
        for field_name, spec in mapping_raw.items():
            if field_name not in MAPPABLE_FIELDS:
                raise ProfileError(
                    f"map 에 알 수 없는 필드 '{field_name}'{where} — "
                    f"사용 가능: {', '.join(MAPPABLE_FIELDS)}"
                )
            if isinstance(spec, str):        # `actor: "Signed by"` 축약형
                spec = {"column": spec}
            if not isinstance(spec, dict) or not spec.get("column"):
                raise ProfileError(f"map.{field_name} 에 column 이 필요합니다{where}")
            spec = dict(spec)
            if spec.get("pattern"):
                # 한 칸에 여러 값이 묻혀 있는 장비가 있다
                # (예: Details = "... Now: 1.000 Before: 0.500").
                # 그 해체 규칙도 코드가 아니라 YAML 에 둔다.
                try:
                    spec["_re"] = re.compile(str(spec["pattern"]))
                except re.error as exc:
                    raise ProfileError(
                        f"map.{field_name}.pattern 정규식 오류{where}: {exc}"
                    ) from exc
            mapping[field_name] = spec

        missing = [f for f in REQUIRED_FIELDS if f not in mapping]
        if missing:
            raise ProfileError(f"map 에 필수 필드가 없습니다{where}: {', '.join(missing)}")

        # blocks 는 헤더가 없어 실행 전에 컬럼 검증이 불가능하므로 여기서 잡는다.
        if read_format == "blocks":
            unknown = {
                f: spec["column"]
                for f, spec in mapping.items()
                if spec["column"] not in block_fields
            }
            if unknown:
                detail = ", ".join(f"{k} → '{v}'" for k, v in unknown.items())
                raise ProfileError(
                    f"map 이 read.fields 에 없는 키를 가리킵니다{where}: {detail}\n"
                    f"  read.fields: {', '.join(block_fields)}"
                )

        # -- vocabulary -----------------------------------------------------
        vocabulary_raw = raw.get("vocabulary") or {}
        if not isinstance(vocabulary_raw, dict):
            raise ProfileError(f"vocabulary 는 매핑이어야 합니다{where}")
        vocabulary: dict[str, dict[str, str]] = {}
        for field_name, table in vocabulary_raw.items():
            if not isinstance(table, dict):
                raise ProfileError(f"vocabulary.{field_name} 는 매핑이어야 합니다{where}")
            vocabulary[field_name] = {
                str(k).strip().lower(): str(v).strip() for k, v in table.items()
            }
        for source_value, mapped in vocabulary.get("action", {}).items():
            if mapped.lower() not in ACTIONS:
                raise ProfileError(
                    f"vocabulary.action 의 '{source_value}' → '{mapped}' 는 "
                    f"허용되지 않는 값입니다{where} (허용: {', '.join(sorted(ACTIONS))})"
                )

        # -- output ---------------------------------------------------------
        output_columns = cls._parse_output(raw.get("output"), where)

        return cls(
            id=profile_id,
            name=name,
            header_contains=header_contains,
            read_format=read_format,
            encoding=(str(read["encoding"]) if read.get("encoding") else None),
            mapping=mapping,
            vocabulary=vocabulary,
            delimiter=_resolve_delimiter(read.get("delimiter")),
            skip_rows=int(read.get("skip_rows", 0) or 0),
            block_separator=str(read.get("block_separator") or DEFAULT_BLOCK_SEPARATOR),
            block_fields=block_fields,
            require_fields=_as_str_list(
                read.get("require_fields"), where, "read.require_fields"
            ),
            skip_if_fields=_as_str_list(
                read.get("skip_if_fields"), where, "read.skip_if_fields"
            ),
            output_columns=output_columns,
            search_lines=search_lines,
            source=source,
        )

    @staticmethod
    def _parse_output(raw: Any, where: str) -> tuple[OutputColumn, ...]:
        if not raw:
            return ()
        if not isinstance(raw, dict):
            raise ProfileError(f"output 은 매핑이어야 합니다{where}")
        columns = raw.get("columns")
        if not columns:
            return ()
        if not isinstance(columns, list):
            raise ProfileError(f"output.columns 는 목록이어야 합니다{where}")

        parsed: list[OutputColumn] = []
        seen: set[str] = set()
        for entry in columns:
            if isinstance(entry, str):       # `- "Type"` → 이름과 출처가 같음
                entry = {"name": entry, "from": entry}
            if not isinstance(entry, dict):
                raise ProfileError(f"output.columns 항목이 잘못됐습니다{where}: {entry!r}")
            name = str(entry.get("name") or entry.get("from") or "").strip()
            origin = str(entry.get("from") or entry.get("name") or "").strip()
            if not name or not origin:
                raise ProfileError(
                    f"output.columns 항목에 name/from 이 필요합니다{where}: {entry!r}"
                )
            if name in seen:
                raise ProfileError(f"output.columns 에 중복된 컬럼명{where}: '{name}'")
            seen.add(name)
            parsed.append(
                OutputColumn(
                    name=name,
                    source=origin,
                    time_format=(
                        str(entry["format"]) if entry.get("format") else None
                    ),
                )
            )
        return tuple(parsed)

    # -- 판별 --------------------------------------------------------------

    def matches(self, header_text: str) -> bool:
        haystack = header_text.lower()
        return all(token.lower() in haystack for token in self.header_contains)

    def matches_file(self, path: Path) -> bool:
        return self.matches(sniff_header(path, self.encoding, self.search_lines))

    # -- 파싱 --------------------------------------------------------------

    def parse_file(
        self, path: Path, prepared: tuple[str, str] | None = None
    ) -> ParseResult:
        """파일 하나를 AuditEvent 목록으로 변환한다.

        prepared: 이미 읽어둔 (본문, 인코딩). 판별 단계에서 읽은 것을 넘기면
        같은 PDF 를 두 번 추출하지 않는다.

        레코드 단위 실패는 모아서 보고하고 나머지는 계속 변환한다
        — 한 줄이 깨졌다고 파일 전체를 버리면 분석이 안 된다.
        """
        text, encoding = prepared if prepared else extract_text(path, self.encoding)
        result = ParseResult(path=path, profile_id=self.id, encoding=encoding)
        records = (
            self._read_blocks(text, result)
            if self.read_format == "blocks"
            else self._read_delimited(text, path)
        )
        for line_no, row in records:
            try:
                result.events.append(self._to_event(row, path, line_no))
            except (SchemaError, ProfileError) as exc:
                result.errors.append(RowError(line=line_no, message=str(exc), raw=row))
        return result

    # -- delimited ---------------------------------------------------------

    def _read_delimited(
        self, text: str, path: Path
    ) -> list[tuple[int, dict[str, Any]]]:
        lines = text.splitlines()[self.skip_rows :]
        if not lines:
            raise ProfileError(f"빈 파일입니다: {path}")

        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=self.delimiter)
        if reader.fieldnames:
            reader.fieldnames = [(f or "").strip() for f in reader.fieldnames]
        self._check_columns(reader.fieldnames or [], path)

        records: list[tuple[int, dict[str, Any]]] = []
        for offset, row in enumerate(reader, start=2):
            clean = {(k or "").strip(): v for k, v in row.items() if k is not None}
            if not any(str(v or "").strip() for v in clean.values()):
                continue                       # 완전한 빈 줄은 조용히 건너뛴다
            records.append((self.skip_rows + offset, clean))
        return records

    def _check_columns(self, fieldnames: Sequence[str], path: Path) -> None:
        """매핑이 가리키는 컬럼이 실제로 있는지 먼저 확인한다.

        모든 행에서 같은 에러가 반복되는 것보다 파일 단위로 한 번 실패하는 게 낫다.
        """
        available = set(fieldnames)
        missing = {
            f: spec["column"]
            for f, spec in self.mapping.items()
            if spec["column"] not in available
        }
        if not missing:
            return
        detail = ", ".join(f"{k} → '{v}'" for k, v in missing.items())
        raise ProfileError(
            f"프로파일 '{self.id}' 가 요구하는 컬럼이 {path.name} 에 없습니다: {detail}\n"
            f"  파일의 실제 컬럼: {', '.join(fieldnames) or '(없음)'}"
        )

    # -- blocks ------------------------------------------------------------

    def _field_pattern(self) -> re.Pattern[str]:
        """'키:' 를 찾는 정규식.

        긴 이름을 먼저 시도한다 — 'Test item' 이 'Test' 보다 앞서야
        짧은 이름이 긴 이름을 잘라먹지 않는다.
        줄 단위가 아니라 위치 기준으로 찾기 때문에,
        값과 다음 키가 한 줄에 붙어 나와도('… – No user – Date: 10.Feb…') 정상 분리된다.
        """
        names = sorted(self.block_fields, key=len, reverse=True)
        # 키 안의 공백은 어떤 공백이든 허용한다 — PDF 추출본은 낱말 사이가
        # 탭이거나 공백 두 칸인 경우가 흔하다 ('Signed\tby:').
        alternatives = "|".join(
            r"\s+".join(re.escape(word) for word in n.split()) for n in names
        )
        return re.compile(rf"(?:(?<=\s)|^)({alternatives})[ \t]*:[ \t]*")

    def _read_blocks(
        self, text: str, result: ParseResult
    ) -> list[tuple[int, dict[str, Any]]]:
        separator = re.compile(self.block_separator)
        pattern = self._field_pattern()

        records: list[tuple[int, dict[str, Any]]] = []
        offset = 0
        for chunk in separator.split(text):
            line_no = text.count("\n", 0, offset) + 1
            offset += len(chunk)
            fields = self._parse_block(chunk, pattern)
            if not fields:
                continue
            if any(k in fields for k in self.skip_if_fields):
                result.skipped += 1            # 페이지 머리글 등
                continue
            if any(k not in fields for k in self.require_fields):
                result.skipped += 1
                continue
            records.append((line_no, fields))
        return records

    @staticmethod
    def _parse_block(chunk: str, pattern: re.Pattern[str]) -> dict[str, Any]:
        """블록 하나를 {키: 값} 으로. 값 안의 줄바꿈은 공백으로 합친다.

        줄바꿈을 합치는 이유: Comment 처럼 폭이 좁아 잘린 값이
        원래 한 문장이었기 때문이다.
        """
        matches = list(pattern.finditer(chunk))
        if not matches:
            return {}
        fields: dict[str, Any] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(chunk)
            value = " ".join(chunk[match.end() : end].split())
            # 키에 섞인 탭/연속 공백을 없애 read.fields 에 적은 이름과 같게 만든다.
            key = " ".join(match.group(1).split())
            if key in fields and fields[key]:     # 같은 키가 두 번이면 이어붙인다
                fields[key] = f"{fields[key]} {value}".strip()
            else:
                fields[key] = value
        return fields

    # -- 공통 변환 ---------------------------------------------------------

    def _to_event(self, row: dict[str, Any], path: Path, line_no: int) -> AuditEvent:
        values = {f: self._extract(f, spec, row) for f, spec in self.mapping.items()}

        timestamp_text = str(values.get("timestamp") or "").strip()
        if not timestamp_text:
            raise SchemaError(
                f"timestamp 로 지정한 '{self.mapping['timestamp']['column']}' 가 비어 있습니다."
            )
        timestamp = _parse_timestamp(
            timestamp_text, _as_format_list(self.mapping["timestamp"].get("format"))
        )

        return AuditEvent(
            timestamp=timestamp,
            actor=values.get("actor"),
            action=values.get("action"),
            target=values.get("target"),
            old_value=values.get("old_value"),
            new_value=values.get("new_value"),
            reason=values.get("reason"),
            equipment_id=self.id,
            source_file=str(path),
            # '__' 로 시작하는 키는 파서가 붙인 메타데이터. 원본 필드와 구분된다.
            raw={**row, "__line__": line_no},
        )

    def _extract(self, field_name: str, spec: dict[str, Any], row: dict[str, Any]) -> Any:
        """원본 칸에서 값 하나를 꺼낸다. pattern 이 있으면 그 부분만 뽑는다.

        pattern 이 안 맞으면 '값 없음'으로 본다 — 예를 들어 설정 변경이 아닌
        레코드에는 'Before:' 가 없으므로 이전값은 비어 있는 게 맞다.
        """
        value = row.get(spec["column"])
        regex: re.Pattern[str] | None = spec.get("_re")
        if regex is not None:
            match = regex.search("" if value is None else str(value))
            if match is None:
                return None
            # 같은 값이 장비 화면마다 다른 문구로 나오는 경우가 있어
            # (Before: / Old program name:) 패턴을 여러 갈래로 쓸 수 있게 한다.
            # 여러 갈래 중 실제로 걸린 쪽의 값을 집는다.
            groups = [g for g in match.groups() if g is not None]
            value = groups[0] if groups else match.group(0)
        return self._translate(field_name, value)

    def _translate(self, field_name: str, value: Any) -> Any:
        """vocabulary 로 장비 용어를 공통 용어로 바꾼다.

        action 만 엄격하다 — 매핑되지 않은 값은 통과시키지 않고,
        YAML 어디를 고쳐야 하는지 값과 함께 알려준다.
        조용히 버리면 위반을 놓친다.
        """
        table = self.vocabulary.get(field_name)
        if not table:
            return value
        key = "" if value is None else str(value).strip().lower()
        if key in table:
            return table[key]
        if field_name == "action":
            raise ProfileError(
                f"vocabulary.action 에 '{value}' 매핑이 없습니다 "
                f"(프로파일 '{self.id}'). "
                f"{self.source.name if self.source else 'YAML'} 에 "
                f'"{value}": <{"|".join(sorted(ACTIONS))}> 을 추가하세요.'
            )
        return value

    # -- 출력 --------------------------------------------------------------

    def columns(self) -> tuple[str, ...]:
        """이 장비 산출물의 컬럼명. output 을 정의하지 않았으면 기본 표를 쓴다."""
        return tuple(c.name for c in self.output_columns) or DEFAULT_COLUMNS

    def row_for(self, event: AuditEvent) -> dict[str, Any]:
        """AuditEvent 하나를 이 장비의 출력 행으로."""
        if not self.output_columns:
            return _default_row(event)
        return {c.name: _column_value(event, c) for c in self.output_columns}


#: output 을 정의하지 않은 프로파일이 쓰는 기본 컬럼.
DEFAULT_COLUMNS: Final[tuple[str, ...]] = (
    "발생일시", "장비", "행위자", "행위",
    "대상", "이전값", "변경값", "사유",
    "원본파일", "원본행번호",
)


def _column_value(event: AuditEvent, column: OutputColumn) -> Any:
    """출력 컬럼 하나의 값. 공통 스키마 이름이면 이벤트에서, 아니면 원본 필드에서."""
    source = column.source
    if source == "timestamp":
        return event.timestamp.strftime(column.time_format or DEFAULT_OUTPUT_TIME_FORMAT)
    if source == "source_name":
        return Path(event.source_file).name if event.source_file else ""
    if source == "line":
        return event.raw.get("__line__", "")
    if source in SCHEMA_SOURCES:
        return getattr(event, source, None) or ""
    return event.raw.get(source, "")          # 원본 필드


def _default_row(event: AuditEvent) -> dict[str, Any]:
    """output 을 정의하지 않은 프로파일용 기본 표."""
    return {
        "발생일시": event.timestamp.strftime(DEFAULT_OUTPUT_TIME_FORMAT),
        "장비": event.equipment_id,
        "행위자": event.actor,
        "행위": event.action,
        "대상": event.target,
        "이전값": event.old_value or "",
        "변경값": event.new_value or "",
        "사유": event.reason or "",
        "원본파일": Path(event.source_file).name if event.source_file else "",
        "원본행번호": event.raw.get("__line__", ""),
    }


# --------------------------------------------------------------------------
# 디렉터리 로딩
# --------------------------------------------------------------------------


def load_profiles(directory: Path | str = "config/profiles") -> list[Profile]:
    """디렉터리의 *.yaml / *.yml 을 전부 읽어 Profile 목록으로.

    id 가 겹치면 어느 파일끼리 겹쳤는지 알려주고 실패한다.
    """
    root = Path(directory)
    if not root.is_dir():
        raise ProfileError(f"프로파일 디렉터리가 없습니다: {root.resolve()}")

    paths = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
    if not paths:
        raise ProfileError(f"프로파일이 하나도 없습니다: {root.resolve()}")

    profiles: list[Profile] = []
    seen: dict[str, Path] = {}
    for path in paths:
        profile = Profile.from_yaml(path)
        if profile.id in seen:
            raise ProfileError(
                f"프로파일 id 가 중복됩니다: '{profile.id}'\n"
                f"  - {seen[profile.id]}\n  - {path}"
            )
        seen[profile.id] = path
        profiles.append(profile)
    return profiles
