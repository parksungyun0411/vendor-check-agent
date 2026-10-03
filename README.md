# vendor-check-agent

[![CI](https://github.com/parksungyun0411/vendor-check-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/parksungyun0411/vendor-check-agent/actions/workflows/ci.yml) ![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white) ![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C) ![MCP](https://img.shields.io/badge/MCP_Python_SDK-2.3-5B6770) [![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 한 줄 소개

상호로 국세청·금융위·OpenDART 공공 API를 조회해 거래처를 점검하는 에이전트를 만들고, 공공 API에서 실제로 겪는 실패를 주입해 복구 전략별로 얼마나 회복하는지, 실패를 정상인 것처럼 보고하는 비율은 얼마인지 재는 실험입니다.

## 배경

거래처 점검은 사업자 상태(계속·휴업·폐업), 법인 기본 정보, 상장 여부를 여러 기관에서 모아 맞추는 일입니다. 공공 API는 실패하는 방식이 제각각입니다. OpenDART는 오류를 HTTP 200 본문의 `status` 코드로 돌려주고(직접 확인), 공공데이터포털 게이트웨이는 `OpenAPI_ServiceResponse` XML/JSON 봉투로 오류를 돌려줍니다. 일일 한도 초과도 흔합니다.

이런 실패에서 가장 비싼 사고는 툴이 실패했는데 점검 결과가 "정상"으로 나가는 경우입니다. 이 프로젝트는 그 비율을 따로 잽니다. 비교 대상은 고정 순서 파이프라인(대조군)과 LangGraph 에이전트입니다. 둘은 같은 툴과 같은 복구 코드를 쓰고, 차이는 그래프 구조(동명 법인 확인, 조건 분기, 병렬 조회, 체크포인트 재개)에서만 나도록 했습니다.

10월 범위는 API 3종(국세청 사업자 상태조회, 금융위 기업기본정보, OpenDART 기업개황)입니다. 조달청 나라장터, 공정위 소속회사 조회, Langfuse 트레이싱은 넣지 않았습니다.

## 아키텍처

```text
                        실험 러너 (eval/run.py)
                        시스템 × 복구 전략 × 실패 시나리오 × 회사 → reports/runs/*.jsonl
                                │                                   │
          대조군 (pipeline.py)  ▼                                   ▼  에이전트 (graph.py, LangGraph)
  search_corp → business_status → dart        resolve ─┬─ 후보 0 ─────────────────────────────▶ report
  동명 법인이면 첫 후보, 세 기관 모두 호출              ├─ 동명 2+ ─▶ confirm (HITL interrupt) ──┐
                                │                       └─ 후보 1 ─────────────────────────────────┤
                                │                       fan-out (Send): nts ∥ dart(상장·DART 등록만) ─▶ report (LLM)
                                │                       한도 초과 → interrupt, 체크포인터(SQLite)에서 재개
                                ▼                                   │
                        MCP 클라이언트 (gateway.py) ◀───────────────┘
                                ▼
  MCP 서버 (mcp_server.py): search_corp · fsc_outline · business_status · dart_find_corp · dart_company
                                │  실패는 예외가 아니라 {"ok": false, "error": {"kind": ...}} 로 반환
                                ▼
  실패 주입 프록시 (proxy.py): timeout · http_5xx · error_200 · quota · malformed · partial
                                ▼
  기관 API: api.odcloud.kr (국세청) · apis.data.go.kr (금융위) · opendart.fss.or.kr
            테스트에서는 합성 데이터로 응답하는 가짜 업스트림 (fake_upstream.py)
```

| 구성 | 파일 | 역할 |
|---|---|---|
| 기관 클라이언트 | `src/vendor_check/clients/` | 공개 명세의 필드명으로 요청·정규화, 실패 분류 |
| MCP 서버 | `mcp_server.py`, `tools.py` | 기관별 툴 5개를 MCPServer(stdio)로 노출 |
| 실패 주입 프록시 | `proxy.py` | 기관별·유형별·횟수별 실패 주입, 호출 수 집계, 한도 초기화 |
| 복구 전략 | `recovery.py` | none / retry / fallback / checkpoint, 점검 항목 병합 |
| 대조군 | `pipeline.py` | 고정 순서 파이프라인 |
| 에이전트 | `graph.py`, `llm.py` | 분기·병렬·HITL·체크포인터, 보고서 작성 LLM(공급자 인터페이스) |
| 평가 | `eval/` | 정답 생성, 실험 행렬, 지표, 결과표 |

## 규모·성능

구현 규모입니다.

| 항목 | 값 |
|---|---|
| 기관 API | 3종 (국세청 상태조회, 금융위 기업개요, OpenDART 기업개황·고유번호) |
| MCP 툴 | 5개 |
| 점검 항목 | 7개 (상호, 대표자, 법인등록번호, 사업자등록번호, 상장 구분, 납세자 상태, 과세유형) |
| 실패 유형 | 6종 (timeout, http_5xx, error_200, quota, malformed, partial) |
| 실패 시나리오 | 31개 (무주입 1 + 기관 3 × (유형 5 × 일시/지속 2 + 한도 초과 1)) |
| 복구 전략 | 4종 |
| 테스트 | 67개. 가짜 업스트림 위에서 그래프·프록시·MCP 서버를 실제로 실행 |

측정 결과입니다.

측정 전 — `make eval` 실행 후 자동 생성

| 시스템 | 점검 항목 일치율 | 조용한 오답 비율 | 한도 초과 재개 성공률 | 건당 토큰(입/출) | 건당 비용 | p95 지연(s) |
|---|---|---|---|---|---|---|
| 고정 파이프라인 |  |  |  |  |  |  |
| LangGraph 에이전트 |  |  |  |  |  |  |

실패 유형 × 복구 전략 표는 `make eval` 이 `reports/RESULTS.md` 에 만듭니다. 테스트가 합성 데이터로 확인하는 값은 단언일 뿐이라 이 표에 넣지 않습니다.

지표 정의 (`src/vendor_check/eval/metrics.py`):

- 점검 항목 일치율: (점검, 항목) 쌍 중 정상으로 보고했고 값이 정답과 같은 비율
- 점검 성공률: 7개 항목이 모두 정답과 같은 점검의 비율
- 조용한 오답 비율: 정상으로 보고한 항목 중 값이 정답과 다른 비율
- 재개 성공률: 한도 초과로 멈춘 점검 중 재개 후 점검에 성공한 비율
- 건당 토큰·비용·p95: LLM 사용량과 점검 한 건의 벽시계 시간. 단가는 `.env` 에 직접 넣어야 비용이 계산됩니다

## 핵심 설계 판단

- **실패를 데이터로 넘깁니다.** 툴은 예외를 던지지 않고 `{"ok": false, "error": {"kind", "code", "retryable"}}` 를 돌려줍니다. MCP 경계를 넘어도 실패 분류가 남아야 대조군과 에이전트가 같은 기준으로 복구를 고를 수 있습니다.
- **오류 판별은 HTTP 상태가 아니라 본문 기준입니다.** OpenDART는 오류를 HTTP 200으로 줍니다. 공공데이터포털은 이번 관찰에서 인증 오류를 401/403으로 줬지만, 한도 초과 때의 상태 코드는 확인하지 못했습니다. 그래서 `OpenAPI_ServiceResponse` 봉투, DART `status`, 필수 필드 누락을 상태 코드와 무관하게 검사합니다(strict). 비교 조건으로 HTTP 상태만 보는 lenient 모드를 두었고, lenient에서 200 오류 본문이 "결과 없음 → 미등록"으로 바뀌는 경로를 테스트로 고정했습니다.
- **복구 전략은 섞지 않습니다.** none·retry·fallback·checkpoint를 각각 단독으로 적용합니다. retry는 재시도할 수 있는 실패(timeout, 5xx, 게이트웨이 01·05, malformed, partial, 초당 한도)만 재시도하고, 일일 한도와 인증 오류는 재시도하지 않습니다. fallback은 금융위가 실패하면 OpenDART 고유번호 목록에서 후보를 찾고, 그 사업자번호로 국세청 조회까지 이어 갑니다. 전략을 조합했을 때의 효과는 재지 않습니다.
- **재개 단위는 기관 노드입니다.** 국세청과 OpenDART 조회를 `Send` 로 같은 superstep 에서 돌리기 때문에, 국세청이 한도 초과로 멈춰도 같이 끝난 OpenDART 결과는 체크포인트에 남습니다. 재개하면 멈춘 노드만 다시 부릅니다. 테스트에서 재개 전후 프록시의 업스트림 호출 수로 이를 확인합니다. 대조군은 같은 상황에서 처음부터 다시 돕니다.
- **동명 법인은 사람이 고릅니다.** 후보가 둘 이상이면 `confirm` 노드가 `interrupt` 로 멈추고 법인등록번호를 받습니다. 실험에서는 정답의 사업자번호로 답하는 자동 승인자를 씁니다. 대조군은 첫 후보를 고르므로 동명 법인에서 다른 법인을 점검할 수 있고, 이 차이도 지표에 들어갑니다.
- **분기는 규칙으로 정합니다.** 금융위 결과가 비상장이고 `fssCorpUnqNo` 도 비어 있으면 OpenDART를 부르지 않고 상장 구분을 금융위 값으로 확정합니다. LLM은 경로를 정하지 않고, 병합된 초안과 실패 기록을 받아 최종 보고서와 요약을 씁니다. LLM이 값을 바꾸거나 실패한 항목을 채우면 조용한 오답에 그대로 잡힙니다.
- **LLM은 공급자 인터페이스 뒤에 둡니다.** 테스트는 초안을 그대로 돌려주는 결정적 스텁을 씁니다. Anthropic 구현은 구조화 출력(JSON 스키마)과 서버 측 거절 폴백을 쓰고, 그래도 거절되면 초안을 그대로 씁니다.

## 실행 방법

필요한 것: Python 3.12, [uv](https://docs.astral.sh/uv/), 공공데이터포털 인증키(국세청 15081808, 금융위 15043184 활용신청), OpenDART 인증키. LLM 보고서를 실제로 쓰려면 Anthropic API 키가 필요합니다.

```bash
uv sync --extra dev --extra llm
cp .env.example .env          # 키 입력
make test lint                # 키 없이 동작
```

| 타깃 | 하는 일 | 키 |
|---|---|---|
| `make mcp` | 기관별 툴 MCP 서버(stdio) | 필요 |
| `make proxy` | 실패 주입 프록시 단독 실행(`FAULT_PLAN=faults/example_plan.json`). MCP 서버의 `*_BASE_URL` 을 프록시 주소로 바꿔 씁니다 | 필요 |
| `make check NAME=상호` | 한 곳 대화형 점검. 동명 법인이면 터미널에서 고르고, 한도 초과로 멈추면 `python -m vendor_check.cli resume <thread_id>` 로 이어 갑니다 | 필요 |
| `make groundtruth` | `data/companies.csv` 의 회사를 프록시 없이 직접 조회해 `data/ground_truth.jsonl` 생성 | 필요 |
| `make baseline` / `make agent` | 실패 주입 없이 대조군 / 에이전트 실행 | 필요 |
| `make inject` | 시나리오 31개 × 전략 4종 × 두 시스템 | 필요 |
| `make eval` | `reports/runs/*.jsonl` → `reports/metrics.json` → `reports/RESULTS.md` | 불필요 |
| `make report` | `reports/metrics.json` 으로 결과표만 다시 생성 | 불필요 |

`data/companies.csv` 는 직접 만들어야 합니다(열: `id,corp_name,bzno`, 형식은 `data/companies.example.csv`). 상장사, 비상장 법인, 휴·폐업 법인, 동명 법인을 섞어 100곳을 고를 계획입니다. 금융위 개발계정 한도가 10,000건/일이라 전체 행렬(100곳 × 31 × 4 × 2 = 24,800회 점검)은 하루에 끝나지 않습니다. `--scenarios`, `--limit` 으로 나눠 돌리면 기록이 같은 파일에 이어 붙습니다.

## 재현 (reports/)

| 경로 | 내용 | 커밋 여부 |
|---|---|---|
| `reports/runs/*.jsonl` | 점검 한 건당 한 줄: 시스템, 전략, 시나리오, 보고서, 기관별 호출 수, 토큰, 지연 | 커밋하지 않음 |
| `reports/metrics.json` | 집계 지표 | 커밋하지 않음 |
| `reports/RESULTS.md` | 결과표. 실측 전에는 빈 표 | 커밋 |
| `docs/api_formats.md` | 기관별 응답·오류 형식, 확인/미확인 구분 | 커밋 |

순서: `make groundtruth` → `make baseline agent inject` → `make eval`.

## 한계

- 측정 결과가 아직 없습니다. 키를 발급받아 정답 생성과 실험 행렬을 돌려야 위 표가 채워집니다.
- 정답은 장애를 주입하지 않은 같은 API 호출로 만듭니다. 기관 원본이 틀렸거나 기관끼리 값이 다르면(대표자 표기 등) 그 차이가 정답에 그대로 들어가고, fallback이 다른 기관 값을 쓰면 불일치로 집계됩니다.
- 공공데이터포털 게이트웨이가 한도 초과·내부 오류를 실제로 HTTP 200으로 주는지는 재현하지 못했습니다. 프록시의 `error_200`·`quota` 는 이 형식을 가정해 만들었고 상태 코드를 바꿀 수 있습니다. 그 밖의 미확인 항목(금융위 JSON 최상위 키와 정상 `resultCode`, `corpRegMrktDcdNm` 값, `fssCorpUnqNo` 와 DART 고유번호의 관계, 미등록 번호의 `b_stt_cd`, 국세청 API의 한도 초과 응답)은 `docs/api_formats.md` 와 코드 주석에 적었습니다.
- 동명 법인 확인은 실험에서 정답으로 답하는 자동 승인자가 대신합니다. 사람이 고를 때의 오류는 재지 않습니다.
- 실패는 회사 한 곳을 점검할 때마다 새로 겁니다. 여러 기관이 동시에 실패하는 경우나 점검 사이에 이어지는 장애는 시나리오에 없습니다.
- 나라장터(조달 등록·부정당제재)와 공정위 소속회사 조회, Langfuse 트레이싱은 구현하지 않았습니다.

## 라이선스

MIT
