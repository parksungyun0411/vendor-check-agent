"""금융위원회 기업기본정보 기업개요조회 (data.go.kr 15043184, getCorpOutline_V2).

확인한 것 (상세 페이지에 내장된 Swagger):
- GET {base}/getCorpOutline_V2, 쿼리 ServiceKey·pageNo·numOfRows·resultType(필수), crno·corpNm(선택)
- header.resultCode / header.resultMsg, body.numOfRows / pageNo / totalCount / items.item
- item 필드: crno, corpNm, enpRprFnm, bzno, corpRegMrktDcd, corpRegMrktDcdNm, fssCorpUnqNo,
  fssCorpChgDtm 등

UNVERIFIED:
- JSON 응답의 최상위 키가 "response" 인지 (Swagger에는 xml name "response"만 있다).
- 정상 resultCode 값. data.go.kr 관례인 "00"으로 가정했다.
- 결과가 1건일 때 items.item 이 객체로 오는지 배열로 오는지. 둘 다 받는다.
- corpRegMrktDcdNm 의 실제 값 목록. market 정규화는 문자열 포함 여부로만 한다.
- 같은 법인이 여러 행으로 오는지. crno 기준으로 fssCorpChgDtm 이 가장 늦은 행을 남긴다.
"""

from __future__ import annotations

from vendor_check.clients.base import AgencyClient
from vendor_check.errors import FailureKind

OK_RESULT_CODES = {"00"}


def normalize_market(name: str | None) -> str | None:
    if not name:
        return None
    if "코스닥" in name or "KOSDAQ" in name.upper():
        return "KOSDAQ"
    if "코넥스" in name or "KONEX" in name.upper():
        return "KONEX"
    if "유가" in name or "KOSPI" in name.upper():
        return "KOSPI"
    return "NONE"


class FscClient(AgencyClient):
    agency = "fsc"

    async def corp_outline(self, corp_name: str | None = None, crno: str | None = None,
                           rows: int = 20) -> list[dict]:
        params = {"ServiceKey": self.key, "pageNo": "1", "numOfRows": str(rows),
                  "resultType": "json"}
        if corp_name:
            params["corpNm"] = corp_name
        if crno:
            params["crno"] = crno
        resp = await self.request("GET", "getCorpOutline_V2", params=params)
        body = self.decode_json(resp)
        root = body.get("response", {}) if isinstance(body, dict) else {}
        if self.strict:
            self.require(body, ["response"], "outline")
            self.require(root, ["header", "body"], "outline.response")
            code = str(root["header"].get("resultCode", ""))
            if code not in OK_RESULT_CODES:
                raise self.fail(FailureKind.GATEWAY, root["header"].get("resultMsg", ""), code)
        items = (root.get("body") or {}).get("items") or {}
        rows_ = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(rows_, dict):
            rows_ = [rows_]

        latest: dict[str, dict] = {}
        for it in rows_:
            self.require(it, ["crno", "corpNm"], "outline item")
            key = it.get("crno") or it.get("corpNm", "")
            prev = latest.get(key)
            if prev is None or str(it.get("fssCorpChgDtm", "")) >= str(prev.get("fssCorpChgDtm", "")):
                latest[key] = it
        return [
            {
                "crno": it.get("crno") or None,
                "corp_name": it.get("corpNm") or None,
                "ceo_name": it.get("enpRprFnm") or None,
                "bzno": it.get("bzno") or None,
                "market": normalize_market(it.get("corpRegMrktDcdNm")),
                "fss_corp_code": it.get("fssCorpUnqNo") or None,
                "address": it.get("enpBsadr") or None,
            }
            for it in latest.values()
        ]
