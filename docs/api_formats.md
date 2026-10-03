# 기관 API 응답·오류 형식 정리

2026-10-03 기준. "확인"은 공개 명세를 읽었거나 키 없이/임의 키로 직접 호출해 본 것이고,
"미확인"은 발급 키로 정상 호출을 해 봐야 알 수 있는 것입니다. 미확인 항목은 코드에도
`UNVERIFIED` 주석으로 표시했습니다.

## 국세청 사업자등록 상태조회 (data.go.kr 15081808)

| 항목 | 내용 | 근거 |
|---|---|---|
| 엔드포인트 | `POST https://api.odcloud.kr/api/nts-businessman/v1/status` | Swagger (infuser.odcloud.kr/api/stages/28493/api-docs) |
| 인증 | 쿼리 `serviceKey` 또는 헤더 `Authorization` | Swagger securityDefinitions |
| 요청 | `{"b_no": ["0000000000", ...]}`, 최대 100개 | Swagger, 413 `TOO_LARGE_REQUEST` |
| 응답 | `status_code`, `match_cnt`, `request_cnt`, `data[]` (`b_no`, `b_stt`, `b_stt_cd`, `tax_type`, `tax_type_cd`, `end_dt`, `utcc_yn`, `tax_type_change_dt`, `invoice_apply_dt`, `rbf_tax_type`, `rbf_tax_type_cd`) | Swagger |
| `b_stt_cd` | 01 계속사업자, 02 휴업자, 03 폐업자 | Swagger |
| 미등록 번호 | `tax_type` = "국세청에 등록되지 않은 사업자등록번호입니다" | Swagger |
| 오류 응답 | 400 `BAD_JSON_REQUEST`, 411 필수 파라미터 누락, 413 `TOO_LARGE_REQUEST`, 500 `INTERNAL_ERROR`, -5 `HTTP_ERROR` | Swagger |
| 인증 오류 | 키 없음: HTTP 401 `{"code":-401,"msg":"인증키는 필수 항목 입니다."}` / 임의 키: HTTP 401 `{"code":-4,"msg":"등록되지 않은 인증키 입니다."}` | 직접 호출 |
| 미확인 | 미등록 번호일 때 `b_stt_cd` 값, 일일 한도 초과 시 응답 형식 | — |

## 금융위원회 기업기본정보 (data.go.kr 15043184)

| 항목 | 내용 | 근거 |
|---|---|---|
| 엔드포인트 | `GET https://apis.data.go.kr/1160100/service/GetCorpBasicInfoService_V2/getCorpOutline_V2` | 상세 페이지 내장 Swagger |
| 파라미터 | `ServiceKey`, `pageNo`, `numOfRows`, `resultType`(xml/json) 필수, `crno`, `corpNm` 선택 | Swagger |
| 응답 | `header.resultCode/resultMsg`, `body.numOfRows/pageNo/totalCount/items.item` | Swagger |
| item 필드(사용) | `crno`, `corpNm`, `enpRprFnm`, `bzno`, `corpRegMrktDcd`, `corpRegMrktDcdNm`, `fssCorpUnqNo`, `fssCorpChgDtm`, `enpBsadr` | Swagger |
| 트래픽 | 개발계정 10,000건/일, 자동승인 | 상세 페이지 |
| 게이트웨이 오류 | `OpenAPI_ServiceResponse > cmmMsgHeader > errMsg / returnAuthMsg / returnReasonCode` | 직접 호출 |
| 오류 코드 | 01 APPLICATION_ERROR, 04 HTTP_ERROR, 05 SERVICETIMEOUT_ERROR, 10 INVALID_REQUEST_PARAMETER_ERROR, 12 NO_OPENAPI_SERVICE_ERROR, 20 SERVICE_KEY_IS_NULL·PERMISSION_DENIED·SERVICE_ACCESS_DENIED_ERROR, 22 일일 한도 초과, 23 초당 한도 초과, 29 BLACKLIST_IP_ACCESS_ERROR, 30 SERVICE_KEY_IS_NOT_REGISTERED_ERROR, 31 DEADLINE_HAS_EXPIRED_ERROR | 상세 페이지 표 |
| 관찰한 상태 코드 | 키 없음 → HTTP 401(JSON 봉투, resultType=json), 임의 키 → HTTP 403(XML 또는 JSON 봉투) | 직접 호출 |
| 미확인 | JSON 응답 최상위 키 `response`, 정상 `resultCode` "00", 1건일 때 `item` 이 객체인지, `corpRegMrktDcdNm` 값 목록, `fssCorpUnqNo` 가 OpenDART `corp_code` 와 같은지, 한도 초과(22)·내부 오류(01·05)의 HTTP 상태 | — |

## OpenDART 기업개황·고유번호

| 항목 | 내용 | 근거 |
|---|---|---|
| 기업개황 | `GET https://opendart.fss.or.kr/api/company.json?crtfc_key&corp_code` → `status`, `message`, `corp_name`, `corp_name_eng`, `stock_name`, `stock_code`, `ceo_nm`, `corp_cls`, `jurir_no`, `bizr_no`, `adres`, `hm_url`, `ir_url`, `phn_no`, `fax_no`, `induty_code`, `est_dt`, `acc_mt` | 개발가이드 DS001/2019002 |
| `corp_cls` | Y 유가, K 코스닥, N 코넥스, E 기타 | 개발가이드 |
| 고유번호 | `GET .../corpCode.xml?crtfc_key` → ZIP, `corp_code`, `corp_name`, `corp_eng_name`, `stock_code`, `modify_date` | 개발가이드 DS001/2019018 |
| 상태 코드 | 000 정상, 010 미등록 키, 011 사용 불가 키, 012 접근 불가 IP, 013 데이터 없음, 014 파일 없음, 020 요청 제한 초과, 021 회사 수 초과, 100 부적절한 필드 값, 101 부적절한 접근, 800 점검 중, 900 정의되지 않은 오류, 901 개인정보 보유기간 만료 키 | 개발가이드 |
| 오류 전달 방식 | **HTTP 200** + `{"status":"010","message":"등록되지 않은 인증키입니다."}` (json), `<result><status>010</status>...` (xml, corpCode 포함) | 직접 호출 |

## 이 정리에서 실험 설계로 이어진 것

- OpenDART 는 오류를 HTTP 200 본문으로 돌려준다는 것을 직접 확인했습니다. HTTP 상태만 보는 클라이언트는
  이 오류를 정상 응답으로 읽습니다. 그래서 `DETECTION_MODE=lenient` 를 비교 조건으로 두었습니다.
- data.go.kr 게이트웨이는 이번 관찰에서 인증 오류를 401/403 으로 돌려줬습니다. "HTTP 200 + XML 오류"는
  커리큘럼 조사 단계의 보고이고 이번에 재현하지 못했습니다. 판별을 상태 코드가 아닌 본문 기준으로 하고,
  프록시의 `error_200`·`quota` 상태 코드를 바꿀 수 있게 둔 이유입니다.
