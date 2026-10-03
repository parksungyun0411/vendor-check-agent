"""국세청 사업자등록 상태조회 (data.go.kr 15081808, api.odcloud.kr).

필드명·응답 코드는 공개 Swagger(infuser.odcloud.kr/api/stages/28493/api-docs)에서 확인했다.
- POST {base}/status, body {"b_no": [...]} (최대 100개, 초과 시 413 TOO_LARGE_REQUEST)
- 응답 status_code "OK", data[].b_no / b_stt / b_stt_cd / tax_type / tax_type_cd / end_dt ...
- 인증 오류는 HTTP 401 + {"code": -4, "msg": ...} 로 관찰했다 (2026-10-03).

UNVERIFIED:
- 미등록 번호일 때 b_stt_cd 값. Swagger는 tax_type 문구만 명시한다. 여기서는 tax_type 문구로
  미등록을 판정하고 b_stt_cd는 받은 그대로 둔다.
- 일일 한도 초과 시 odcloud 의 응답 형식(상태 코드·본문). 비-2xx면 상태로 분류되고,
  200에 data.go.kr 봉투가 오면 게이트웨이 판별로 잡힌다.
"""

from __future__ import annotations

from vendor_check.clients.base import AgencyClient
from vendor_check.errors import FailureKind

UNREGISTERED_MSG = "국세청에 등록되지 않은 사업자등록번호입니다"


def normalize_bno(b_no: str) -> str:
    return "".join(ch for ch in b_no if ch.isdigit())


class NtsClient(AgencyClient):
    agency = "nts"

    async def status(self, b_nos: list[str]) -> dict[str, dict]:
        b_nos = [normalize_bno(b) for b in b_nos]
        resp = await self.request(
            "POST", "status",
            params={"serviceKey": self.key, "returnType": "JSON"},
            json={"b_no": b_nos},
        )
        body = self.decode_json(resp)
        if self.strict:
            if not isinstance(body, dict):
                raise self.fail(FailureKind.MALFORMED, "body is not an object")
            if "code" in body and "status_code" not in body:
                raise self.fail(FailureKind.GATEWAY, str(body.get("msg")), str(body.get("code")))
            self.require(body, ["status_code", "data"], "status response")
            if body["status_code"] != "OK":
                raise self.fail(FailureKind.GATEWAY, "status_code", str(body["status_code"]))
        out: dict[str, dict] = {}
        for item in (body.get("data") or []) if isinstance(body, dict) else []:
            self.require(item, ["b_no", "b_stt_cd", "tax_type"], "status item")
            tax_type = item.get("tax_type") or ""
            out[normalize_bno(item.get("b_no", ""))] = {
                "b_no": normalize_bno(item.get("b_no", "")),
                "registered": not tax_type.startswith(UNREGISTERED_MSG),
                "b_stt_cd": item.get("b_stt_cd") or None,
                "b_stt": item.get("b_stt") or None,
                "tax_type_cd": item.get("tax_type_cd") or None,
                "end_dt": item.get("end_dt") or None,
            }
        missing = [b for b in b_nos if b not in out]
        if missing and self.strict:
            raise self.fail(FailureKind.PARTIAL, f"no result rows for {missing}")
        for b in missing:
            # lenient: 결과 행이 없으면 "미등록"으로 읽는 흔한 처리. 조용한 오답의 출처다.
            out[b] = {"b_no": b, "registered": False, "b_stt_cd": None, "b_stt": None,
                      "tax_type_cd": None, "end_dt": None}
        return out
