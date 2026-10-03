"""공공데이터포털 게이트웨이 오류 본문 판별.

확인한 사실 (2026-10-03, 키 없이/임의 키로 직접 호출해 관찰):
- apis.data.go.kr 는 인증 오류를 `OpenAPI_ServiceResponse > cmmMsgHeader` 로 돌려준다.
  resultType=json 이면 JSON, 그 외에는 XML. HTTP 상태는 401(SERVICE_KEY_IS_NULL),
  403(SERVICE_KEY_IS_NOT_REGISTERED_ERROR) 이었다.
- 오류 코드표(01·04·05·10·12·20·22·23·29·30·31)는 data.go.kr 15043184 상세 페이지 표 그대로다.

확인하지 못한 것 (UNVERIFIED):
- 한도 초과(22·23)·내부 오류(01·05)일 때의 HTTP 상태. 커리큘럼 조사에서 "HTTP 200 + XML
  오류 본문" 사례가 보고됐지만 이번에 직접 재현하지는 못했다. 그래서 판별은 HTTP 상태와
  무관하게 본문으로 한다.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

from vendor_check.errors import FailureKind

GATEWAY_CODES: dict[str, FailureKind] = {
    "01": FailureKind.GATEWAY,  # APPLICATION_ERROR
    "04": FailureKind.GATEWAY,  # HTTP_ERROR
    "05": FailureKind.GATEWAY,  # SERVICETIMEOUT_ERROR
    "10": FailureKind.INVALID_REQUEST,  # INVALID_REQUEST_PARAMETER_ERROR
    "12": FailureKind.INVALID_REQUEST,  # NO_OPENAPI_SERVICE_ERROR
    "20": FailureKind.AUTH,  # SERVICE_KEY_IS_NULL / PERMISSION_DENIED / SERVICE_ACCESS_DENIED_ERROR
    "22": FailureKind.QUOTA,  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR
    "23": FailureKind.RATE_LIMIT,  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_PER_SECOND_EXCEEDS_ERROR
    "29": FailureKind.AUTH,  # BLACKLIST_IP_ACCESS_ERROR
    "30": FailureKind.AUTH,  # SERVICE_KEY_IS_NOT_REGISTERED_ERROR
    "31": FailureKind.AUTH,  # DEADLINE_HAS_EXPIRED_ERROR
}

_MARKER = "OpenAPI_ServiceResponse"


def parse_gateway_error(body: bytes) -> tuple[str, str] | None:
    """본문이 게이트웨이 오류 봉투면 (returnReasonCode, errMsg)를, 아니면 None을 돌려준다."""
    text = body.decode("utf-8", errors="replace").strip()
    if _MARKER not in text:
        return None
    if text.startswith("{"):
        try:
            header = json.loads(text)[_MARKER]["cmmMsgHeader"]
            return str(header.get("returnReasonCode", "")), str(header.get("errMsg", ""))
        except (ValueError, KeyError, TypeError):
            pass
    try:
        root = ET.fromstring(text)
        code = root.findtext(".//returnReasonCode") or ""
        msg = root.findtext(".//errMsg") or ""
        return code.strip(), msg.strip()
    except ET.ParseError:
        m = re.search(r"<returnReasonCode>\s*(\d+)\s*<", text)
        return (m.group(1) if m else "", "unparseable gateway envelope")


def classify_gateway_code(code: str) -> FailureKind:
    return GATEWAY_CODES.get(code, FailureKind.GATEWAY)
