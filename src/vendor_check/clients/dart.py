"""OpenDART 기업개황(company.json)과 고유번호(corpCode.xml).

확인한 것 (opendart.fss.or.kr 개발가이드 DS001 2019002·2019018, 2026-10-03):
- GET {base}/company.json?crtfc_key&corp_code → status, message, corp_name, ceo_nm, corp_cls(Y/K/N/E),
  jurir_no, bizr_no, stock_code ...
- GET {base}/corpCode.xml?crtfc_key → ZIP(CORPCODE.xml): corp_code, corp_name, corp_eng_name,
  stock_code, modify_date
- 오류는 HTTP 200 + {"status": "010", "message": ...} 로 온다 (임의 키로 직접 호출해 관찰).
  상태 코드표 000·010·011·012·013·014·020·021·100·101·800·900·901 은 가이드 그대로다.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from vendor_check.clients.base import AgencyClient
from vendor_check.errors import FailureKind

DART_STATUS: dict[str, FailureKind] = {
    "010": FailureKind.AUTH,
    "011": FailureKind.AUTH,
    "012": FailureKind.AUTH,
    "020": FailureKind.QUOTA,
    "021": FailureKind.INVALID_REQUEST,
    "100": FailureKind.INVALID_REQUEST,
    "101": FailureKind.INVALID_REQUEST,
    "800": FailureKind.MAINTENANCE,
    "900": FailureKind.GATEWAY,
    "901": FailureKind.AUTH,
}
NOT_FOUND = "013"
CORP_CLS_MARKET = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "NONE"}


def parse_corpcode_xml(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    return [
        {
            "corp_code": (el.findtext("corp_code") or "").strip(),
            "corp_name": (el.findtext("corp_name") or "").strip(),
            "stock_code": (el.findtext("stock_code") or "").strip() or None,
        }
        for el in root.iter("list")
    ]


class DartClient(AgencyClient):
    agency = "dart"

    def _check_status(self, body: dict) -> bool:
        """정상이면 True, 조회 결과 없음(013)이면 False. 그 밖의 코드는 실패로 올린다."""
        if not self.strict:
            return True
        self.require(body, ["status"], "dart response")
        status = str(body["status"])
        if status == "000":
            return True
        if status == NOT_FOUND:
            return False
        raise self.fail(DART_STATUS.get(status, FailureKind.GATEWAY), body.get("message", ""), status)

    async def company(self, corp_code: str) -> dict | None:
        resp = await self.request("GET", "company.json",
                                  params={"crtfc_key": self.key, "corp_code": corp_code})
        body = self.decode_json(resp)
        if not isinstance(body, dict):
            if self.strict:
                raise self.fail(FailureKind.MALFORMED, "body is not an object")
            body = {}
        if not self._check_status(body):
            return None
        self.require(body, ["corp_name", "corp_cls", "jurir_no", "bizr_no", "ceo_nm"], "company")
        return {
            "corp_code": corp_code,
            "corp_name": body.get("corp_name") or None,
            "ceo_name": body.get("ceo_nm") or None,
            "crno": body.get("jurir_no") or None,
            "bzno": body.get("bizr_no") or None,
            "market": CORP_CLS_MARKET.get(body.get("corp_cls", "")),
            "stock_code": (body.get("stock_code") or "").strip() or None,
        }

    async def download_corpcodes(self, cache: Path | None = None) -> list[dict]:
        if cache is not None and cache.exists():
            return parse_corpcode_xml(cache.read_bytes())
        resp = await self.request("GET", "corpCode.xml", params={"crtfc_key": self.key})
        if not resp.content.startswith(b"PK"):
            # 오류면 ZIP 대신 <result><status>..</status> XML 이 온다 (관찰).
            try:
                root = ET.fromstring(resp.content)
                status = root.findtext("status") or ""
                message = root.findtext("message") or ""
            except ET.ParseError as e:
                raise self.fail(FailureKind.MALFORMED, "corpCode is neither ZIP nor XML") from e
            raise self.fail(DART_STATUS.get(status, FailureKind.GATEWAY), message, status)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                xml_bytes = zf.read(zf.namelist()[0])
        except zipfile.BadZipFile as e:
            raise self.fail(FailureKind.MALFORMED, "broken corpCode ZIP") from e
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(xml_bytes)
        return parse_corpcode_xml(xml_bytes)
