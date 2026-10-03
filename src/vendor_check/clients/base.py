"""기관 API 공통 요청 처리.

strict 모드는 HTTP 상태와 본문을 함께 보고 실패를 분류한다. lenient 모드는 흔한
`raise_for_status()` 수준의 클라이언트를 흉내 낸다: 2xx면 본문을 믿고, JSON이 아니면
빈 dict로 취급한다. lenient는 "본문 검사를 안 하면 얼마나 조용히 틀리는가"를 재기 위한
비교 조건이다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from vendor_check.clients.gateway import classify_gateway_code, parse_gateway_error
from vendor_check.errors import FailureKind, ToolError, ToolFailure


class AgencyClient:
    agency = "base"

    def __init__(
        self,
        base_url: str,
        key: str,
        http: httpx.AsyncClient,
        timeout_s: float = 10.0,
        strict: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.http = http
        self.timeout_s = timeout_s
        self.strict = strict

    def fail(self, kind: FailureKind, message: str = "", code: str | None = None,
             http_status: int | None = None) -> ToolFailure:
        return ToolFailure(ToolError(self.agency, kind, message, code, http_status))

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            # httpx 타임아웃과 별도로 wait_for를 건다. 프록시가 응답을 붙잡아 두는 주입은
            # ASGI 전송에서도 같은 방식으로 끊겨야 한다.
            resp = await asyncio.wait_for(
                self.http.request(method, url, timeout=self.timeout_s, **kwargs),
                timeout=self.timeout_s,
            )
        except (TimeoutError, httpx.TimeoutException) as e:
            raise self.fail(FailureKind.TIMEOUT, f"no response within {self.timeout_s}s") from e
        except httpx.TransportError as e:
            raise self.fail(FailureKind.TRANSPORT, str(e)) from e

        if self.strict:
            gw = parse_gateway_error(resp.content)
            if gw is not None:
                code, msg = gw
                raise self.fail(classify_gateway_code(code), msg, code, resp.status_code)

        if resp.status_code >= 500:
            raise self.fail(FailureKind.HTTP_5XX, resp.text[:200], http_status=resp.status_code)
        if resp.status_code == 429:
            raise self.fail(FailureKind.RATE_LIMIT, resp.text[:200], http_status=429)
        if resp.status_code in (401, 403):
            raise self.fail(FailureKind.AUTH, resp.text[:200], http_status=resp.status_code)
        if resp.status_code >= 400:
            raise self.fail(FailureKind.HTTP_4XX, resp.text[:200], http_status=resp.status_code)
        return resp

    def decode_json(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            if not self.strict:
                return {}
            raise self.fail(FailureKind.MALFORMED, f"invalid JSON: {e}",
                            http_status=resp.status_code) from e

    def require(self, obj: dict, fields: list[str], where: str) -> None:
        if not self.strict:
            return
        missing = [f for f in fields if f not in obj]
        if missing:
            raise self.fail(FailureKind.PARTIAL, f"{where} missing {missing}")
