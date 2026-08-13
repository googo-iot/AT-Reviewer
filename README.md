# AT-Reviewer

제조장비 audit trail 분석 도구.

> **⚠️ 검증되지 않은 개인 분석용 도구입니다.**
> 이 도구의 결과는 참고용이며, 그 자체로 어떤 판정 근거도 되지 않습니다.
> **최종 판정은 반드시 원본 감사추적을 직접 확인해야 합니다.**
> 밸리데이션을 거치지 않았고, 규제 목적의 기록이나 보고에 사용할 수 없습니다.

---

## 목적

장비마다 형식이 제각각인 audit trail을 **하나의 공통 형식으로 변환**하고,
**위반 의심 항목을 찾아내는** 것.

두 가지를 한다.

| | 명령 | 결과 |
|---|---|---|
| **변환** | `convert` | 여러 장비·여러 파일 → 표(CSV) |
| **검사** | `check` | 공통 스키마에 규칙을 적용 → 위반내역(CSV) |
| **둘 다** | `analyze` | 변환 + 검사를 **Excel 한 권**으로 (시트: 위반내역 / 전체이벤트 / 처리요약) |

## 설계 원칙

이 네 가지가 코드 전반의 판단 기준이다.

1. **장비별 파싱 규칙은 코드가 아니라 YAML에 있다.**
   장비를 추가할 때 Python을 건드리지 않는다.
2. **읽는 즉시 공통 스키마(`AuditEvent`)로 변환한다.**
   그 아래로는 장비 차이가 흘러가지 않는다.
3. **위반 규칙은 공통 스키마만 본다.** 장비 종류를 모르고, 알 필요도 없다.
4. **실제 데이터 파일은 저장소에 두지 않는다.** `.gitignore`가 막는다.

여기에 실무에서 나온 원칙이 하나 더 있다.

> **애매하면 추측하지 않고 실패한다.**
> 감사추적을 엉뚱한 프로파일로 읽으면 컬럼이 밀린 채 "정상 처리"되어
> 틀린 결론이 조용히 나온다. 그건 못 읽는 것보다 훨씬 위험하다.

---

## 빠른 시작

```bash
# 1. 의존성
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 분석할 파일을 data/ 에 넣는다 (저장소에 올라가지 않음)

# 3. 공통 표로 변환
python -m src.cli convert data -o "output/감사추적.csv"

# 4. 위반 의심 검사
python -m src.cli check data -o "output/위반내역.csv"

# 또는 — 3~4를 한 번에, Excel 한 권으로
python -m src.cli analyze data -o "output/감사추적분석.xlsx"
```

### 실행 예시

```
$ python -m src.cli convert data -o "output/감사추적.csv"

프로파일 3개 / 입력 20개
  (하위 폴더의 19개는 제외했습니다. 포함하려면 -r)

  2024-07.pdf                  pall_flowstar_v         4건
  2025-04.pdf                  pall_flowstar_v       941건
  ...
  2026-07.pdf                  pall_flowstar_v       536건

중복 제외 699건 → output\감사추적_중복.csv

──────────────────────────────────────────────────────────────
파일      20/20 처리
이벤트    4,061건
무시      1,065건 (페이지 머리글 등 데이터가 아닌 블록)
중복제거  699건 (내보내기 구간이 겹치는 파일)
          2025-04.pdf 의 431건이 2025-05.pdf 와 중복 (→ 2025-05.pdf 를 남김)
기간      2024-07-01 ~ 2026-07-28
행위자    16명
행위      execute 2,773, login 869, config 260, login_failed 73, modify 50, ...
──────────────────────────────────────────────────────────────
  pall_flowstar_v  4,061건  →  output\감사추적.csv
```

```
$ python -m src.cli check data -o "output/위반내역.csv"

──────────────────────────────────────────────────────────────
검사 대상  4,061건
위반 의심  6건
심각도     높음 5, 보통 1

  · audit_trail_disabled     감사추적 기능 해제 정황          0건
  · delete_without_reason    사유 없는 삭제               0건
    system_clock_changed     시스템 시각 변경              5건
  · after_hours_change       업무시간 외 변경·삭제           0건
    repeated_login_failure   단기간 반복 로그인 실패          1건

  [높음] 상위 건
    2025-06-20 09:43  Administrator (ADMIN)  2025-06-20 09:43:26 → 2025-04-29 16:42:24 (51일 17시간 뒤로)
──────────────────────────────────────────────────────────────
```

---

## 새 장비 추가하는 방법 (3줄 요약)

1. `python -m src.cli inspect <새파일>` 로 어떤 프로파일에도 안 걸리는 것을 확인한다.
2. `config/profiles/<장비id>.yaml` 을 한 장 쓴다. **Python은 건드리지 않는다.**
3. `python -m src.cli convert <새파일>` 로 돌린다. 미매핑 값은 에러가 *어디를 고칠지* 알려준다.

<details>
<summary>조금 더 자세히</summary>

**판별이 안 될 때** — `inspect`가 프로파일별로 어느 조건이 맞고 어긋났는지 보여준다.

```
$ python -m src.cli inspect data/알수없는파일.pdf
  [ ] pall_flowstar_v: 일치 [없음] / 누락 [Pall Flowstar V, System Audit Trail]
  [ ] sample_hplc: 일치 [없음] / 누락 [Changed By, Item Name]
```

**미매핑 값이 있을 때** — 조용히 버리지 않고, 고칠 파일과 넣을 값을 알려준다.

```
vocabulary.action 에 'Reviewed' 매핑이 없습니다 (프로파일 'sample_hplc').
sample_hplc.yaml 에 "Reviewed": <backup|config|create|delete|execute|login|
login_failed|modify|sign> 을 추가하세요.
```

**테스트는 저절로 늘어난다** — 프로파일 YAML을 넣으면 구조 검증이 자동으로 걸리고,
`tests/fixtures/<장비id>/` 에 입력 파일과 `expected.csv` 를 두면 파싱 검증까지 붙는다.
테스트 코드는 건드리지 않는다.

</details>

---

## 프로파일 YAML

장비 하나 = YAML 한 장. 입력 형식 두 가지를 지원한다.

### `delimited` — 구분자 표 (CSV/TSV)

```yaml
id: sample_hplc                       # 파일명과 같아야 한다
name: HPLC CDS 감사추적

detect:
  header_contains: ["Changed By", "Item Name"]   # 앞부분에 이 문구가 다 있으면 이 장비

read:
  encoding: cp949                     # 실패하면 utf-8-sig → cp949 → utf-16 순으로 재시도
  delimiter: ","                      # "\t" 또는 tab 도 가능
  skip_rows: 0

map:
  timestamp: {column: "Date/Time", format: "%d-%b-%y %H:%M:%S"}
  actor:     "Changed By"             # 축약형
  action:    {column: "Item Type"}
  target:    {column: "Item Name"}
  old_value: {column: "Old Value"}
  new_value: {column: "New Value"}
  reason:    {column: "Reason"}

vocabulary:                           # 장비 용어 → 공통 어휘
  action:
    "Modified": modify
    "Deleted": delete
    "Sign Off": sign
```

### `blocks` — 구분선으로 나뉜 '키: 값' 리포트 (PDF 내보내기 등)

```yaml
read:
  format: blocks
  block_separator: "[-–—_]{20,}"      # 정규식
  fields: ["Type", "Action", "Details", "Signed by", "Date", "Page"]
  require_fields: ["Date", "Action"]  # 없으면 그 블록은 레코드가 아니다
  skip_if_fields: ["Page"]            # 페이지 머리글 제거
```

### 출력 표 (`output`)

컬럼 구성은 장비마다 다르므로 프로파일이 직접 정한다.
`from` 에는 **공통 스키마 이름** 또는 **원본 필드 이름**을 쓴다.

```yaml
output:
  columns:
    - {name: "발생일시", from: timestamp, format: "%Y-%m-%d %H:%M:%S"}
    - {name: "행위",     from: action}      # 공통 어휘로 정규화된 값
    - {name: "원본행위", from: "Action"}    # 장비가 쓴 원문
    - {name: "구분",     from: "Type"}      # 원본 필드
    - {name: "원본파일", from: source_name}
    - {name: "원본줄번호", from: line}
```

### 한 칸에 여러 값이 묻혀 있을 때 (`pattern`)

`Details: "Allow admins to edit programs Now: All administrators Before: No administrators"`
처럼 한 칸에 이전값·변경값이 문장으로 들어있는 장비가 있다. 해체 규칙도 YAML에 둔다.

```yaml
map:
  new_value: {column: "Details", pattern: "Now:\\s*(.*?)\\s*(?:Before:|$)"}
  old_value: {column: "Details", pattern: "(?:Before:|Old program name:)\\s*(.*)$"}
```

### 공통 스키마 (`AuditEvent`)

| 필드 | 설명 |
|---|---|
| `timestamp` | 발생 시각 (필수, datetime) |
| `actor` | 행위자 (필수) |
| `action` | 아래 9개 중 하나 (필수) |
| `target` | 대상 객체명 |
| `old_value` / `new_value` | 이전값 / 변경값 |
| `reason` | 사유 |
| `equipment_id` / `source_file` | 어느 장비, 어느 파일 |
| `raw` | 원본 레코드 전체 (추적성 보존) |

`action` 어휘: `modify` `delete` `create` `login` `login_failed` `sign` `config` `execute` `backup`

> 어휘를 늘릴 때의 기준은 **"규칙이 이 값만 보고 판단할 수 있는가"** 다.
> 예를 들어 로그인 성공과 실패를 한 값으로 묶으면
> '연속 로그인 실패' 규칙이 공통 스키마만으로는 불가능해진다.

---

## 위반 규칙

`config/rules/default.yaml` 에서 켜고 끄기 · 심각도 · 임계값을 조정한다.
**규칙을 새로 만들 때만** `src/core/rules.py` 를 건드린다 (규칙 하나당 함수 하나).

| 규칙 | 기본 심각도 | 판단 근거 |
|---|---|---|
| `delete_without_reason` | 높음 | `action == delete` 인데 사유가 비어 있음 |
| `audit_trail_disabled` | 높음 | '감사추적' 낱말과 '끔' 낱말이 함께 나옴 |
| `system_clock_changed` | 높음 | 이전값·변경값이 **둘 다 시각으로 읽히고** 차이가 큼 |
| `after_hours_change` | 보통 | 22~06시의 변경·삭제 |
| `repeated_login_failure` | 보통 | 같은 사람 이름으로 30분 내 3회 이상 실패 |

`system_clock_changed` 는 낱말이 아니라 **값의 성질**로 판단하므로,
장비가 그 설정을 뭐라고 부르든 잡힌다. 시각이 바뀌면 그 뒤의 모든 기록이
실제 순서와 어긋나므로 단일 사건 중 가장 무거운 축이다.

모든 findings에는 **근거**가 붙는다.
근거 없이 규칙 이름만 나오면 결국 원본을 다시 뒤져야 하고, 그러면 도구를 쓸 이유가 없다.

---

## 폴더 구조

```
AT-Reviewer/
├─ src/
│  ├─ cli.py                  명령줄 진입점 (convert / check / analyze / profiles / inspect)
│  └─ core/
│     ├─ schema.py            공통 스키마 AuditEvent — 표준 라이브러리만 사용
│     ├─ profile.py           프로파일 로더 + 파서 (delimited / blocks / PDF)
│     ├─ registry.py          장비 자동 판별, 입력 파일 수집
│     ├─ rules.py             위반 규칙 (규칙 하나당 함수 하나)
│     ├─ excel.py             Excel 산출물 (서식은 전부 여기서 책임진다)
│     └─ export.py            표 출력, 중복 판정
├─ config/
│  ├─ profiles/*.yaml         장비 하나당 한 장
│  └─ rules/default.yaml      규칙 설정
├─ tests/
│  ├─ conftest.py             프로파일·픽스처 자동 수집
│  ├─ test_profiles.py        모든 프로파일 구조 검증 (자동 확장)
│  ├─ test_fixtures.py        픽스처 파싱 검증 (자동 확장)
│  ├─ test_schema.py / test_rules.py / test_export.py / test_excel.py
│  └─ fixtures/<장비id>/      입력 + expected.csv
├─ data/                      분석 대상 (저장소에 올라가지 않음)
└─ output/                    산출물 (저장소에 올라가지 않음)
```

---

## 명령

```
python -m src.cli convert <경로> [-o 산출물.csv]     표로 변환
python -m src.cli check   <경로> [-o 위반내역.csv]   변환 후 규칙 적용
python -m src.cli analyze <경로> [-o 분석.xlsx]      변환 + 검사를 Excel 한 권으로
python -m src.cli profiles                          등록된 프로파일 목록
python -m src.cli inspect <파일>                    이 파일이 어느 프로파일에 걸리는지
```

### Excel 산출물 (`analyze`)

| 시트 | 내용 |
|---|---|
| **위반내역** | 규칙에 걸린 항목. 심각도별 색, 머리글 고정, 자동 필터 |
| **전체이벤트** | 변환된 전체 이벤트 (장비가 여럿이면 `전체이벤트_<장비id>` 로 나뉜다) |
| **처리요약** | 실행 정보 · 파일별 처리 결과 · 규칙별 건수 · 행위 분포 |
| **실패내역** | 변환 실패가 있을 때만 생긴다 |

처리요약 시트에는 **대상 경로 · 실행 시각 · 파일별 건수와 기간 · 중복 제외 수**가
들어간다. 산출물만 보고도 "무엇을 어떻게 처리한 결과인지" 알 수 있어야 하기 때문이다.
주의 문구도 이 시트에 함께 들어간다.

**주요 옵션**

| 옵션 | 설명 |
|---|---|
| `-r`, `--recursive` | 하위 폴더까지 훑음 (기본: 지정한 폴더만) |
| `--keep-duplicates` | 파일 간 중복을 제거하지 않고 전부 남김 |
| `--profiles DIR` / `--rules FILE` | 설정 위치 변경 |
| `--encoding` | 산출물 인코딩 (기본 `utf-8-sig` — Excel이 한글을 바로 읽는다) |

**종료 코드** — `0` 전부 성공 / `1` 일부 파일 실패 / `2` 설정 오류·쓰기 실패

---

## 알아둘 동작

**입력** — 폴더를 지정하면 **그 폴더만** 본다. 하위 폴더는 대개 다른 장비의
묶음이라 딸려 들어오면 산출물이 조용히 오염된다. 포함하려면 `-r`.
`.csv` `.tsv` `.txt` `.pdf` 를 읽는다.

**실패** — 한 파일이 실패해도 나머지는 계속 처리한다. 실패는 `_실패.csv` 에
**단계(detect/read/row) + 줄번호 + 원본 레코드**와 함께 남는다.

**중복** — 내보내기 구간이 겹치는 파일을 여러 장 받는 일이 흔하다.
기본적으로 제거하되, **제외한 것은 `_중복.csv` 에 남긴다.** 조용히 줄어드는 산출물이 가장 위험하다.
어느 사본을 남길지는 ① 파일명이 그 사건의 연-월을 담고 있는 쪽 ② 그 달의 비중이 큰 쪽
③ 파일명 순으로 정한다.

**추적성** — 산출물의 아무 행에서나 `원본파일` + `원본줄번호` 로
원본의 몇 번째 줄인지 되짚을 수 있다.

---

## 개발

```bash
.venv/Scripts/python.exe -m pytest          # 116 passed, 4 skipped
```

테스트 픽스처(`tests/fixtures/`)는 **합성 데이터**다.
실제 장비 데이터는 저장소에 들어가지 않으므로, 실제 파일에서 확인된
형식과 함정만 재현해 두었다.

---

## 다시 한 번

**이 도구는 검증되지 않았습니다.** 결과는 검토의 출발점일 뿐이며,
누락과 오탐이 모두 있을 수 있습니다. **최종 판정은 원본 감사추적 확인이 필요합니다.**
