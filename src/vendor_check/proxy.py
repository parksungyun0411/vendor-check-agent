"""실패 주입 프록시.

툴(기관 클라이언트)과 기관 API 사이에 끼는 HTTP 프록시다. 경로 첫 마디가 기관 이름이다:
  /nts/...  → NTS 업스트림,  /fsc/... → 금융위 업스트림,  /dart/... → OpenDART 업스트림

주입하는 실패 유형:
  timeout    응답을 delay_s 만큼 붙잡는다 (클라이언트 타임아웃 유발)
  http_5xx   503 + HTML 본문
  error_200  HTTP 200 + 오류 본문. data.go.kr 계열은 OpenAPI_ServiceResponse XML(code 기본 01),
             OpenDART 는 {"status": "900"} JSON
  quota      일일 한도 초과. data.go.kr 계열은 200 + XML code 22, OpenDART 는 200 + status 020.
             reset_quota() 를 부르기 전까지(= 다음 날) 계속 실패한다
  malformed  업스트림 응답 본문을 절반에서 자른다
  partial    업스트림 JSON 에서 필수 필드 하나를 지운다

UNVERIFIED: data.go.kr 가 한도 초과·내부 오류를 실제로 HTTP 200 으로 주는지는 직접 재현하지
못했다(인증 오류는 401/403 이었다). error_200·quota 의 상태 코드는 FaultRule.status 로 바꿀 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

AGENCIES = ("nts", "fsc", "dart")
FAULT_KINDS = ("timeout", "http_5xx", "error_200", "quota", "malformed", "partial")

GATEWAY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<OpenAPI_ServiceResponse>\n<cmmMsgHeader>\n'
    "  <errMsg>{err}</errMsg>\n  <returnAuthMsg>{err}</returnAuthMsg>\n"
    "  <returnReasonCode>{code}</returnReasonCode>\n</cmmMsgHeader>\n</OpenAPI_ServiceResponse>"
)
GATEWAY_ERR = {"01": "APPLICATION_ERROR", "05": "SERVICETIMEOUT_ERROR",
               "22": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
               "30": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}
DART_MSG = {"900": "정의되지 않은 오류가 발생하였습니다.", "020": "요청 제한을 초과하였습니다.",
            "800": "시스템 점검으로 인한 서비스가 중지 중입니다."}
PARTIAL_DROP = {"nts": "b_stt_cd", "fsc": "corpNm", "dart": "corp_cls"}
REAL_LIMIT_DART = re.compile(r'"status"\s*:\s*"020"|<status>020</status>')


def real_limit(agency: str, resp: httpx.Response) -> tuple[str, bool] | None:
    """업스트림이 실제로 돌려준 한도 초과 신호. 이 상태로 실험을 이어 가면 주입하지 않은 실패가
    결과에 섞이므로, 러너가 보고 멈춘다. (주입한 quota 는 업스트림까지 가지 않아 여기 걸리지 않는다.)

    (내용, 그날을 막을지)를 돌려준다. 공공데이터포털 초당 한도(23)는 잠시 뒤 풀리므로 그날을 막지 않는다."""
    if resp.status_code == 429:
        return f"{agency}: HTTP 429", True
    text = resp.content[:4000].decode("utf-8", "ignore")
    if "LIMITED_NUMBER_OF_SERVICE_REQUESTS_PER_SECOND" in text:
        return f"{agency}: 공공데이터포털 초당 한도 초과", False
    if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in text:
        return f"{agency}: 공공데이터포털 한도 초과", True
    if agency == "dart" and REAL_LIMIT_DART.search(text):
        return "dart: status 020", True
    return None


@dataclass
class FaultRule:
    agency: str  # nts | fsc | dart | *
    kind: str
    after: int = 0  # 이 규칙에 걸리는 호출 중 앞의 after 건은 통과시킨다
    times: int | None = None  # None 이면 계속 실패(지속 실패), n 이면 n 건만 실패(일시 실패)
    code: str | None = None  # error_200 의 오류 코드
    status: int = 200  # error_200 / quota 의 HTTP 상태
    delay_s: float = 30.0  # timeout 의 지연
    seen: int = 0
    fired: int = 0

    def matches(self, agency: str) -> bool:
        return self.agency in ("*", agency)

    def take(self) -> bool:
        self.seen += 1
        if self.seen <= self.after:
            return False
        if self.times is not None and self.fired >= self.times:
            return False
        self.fired += 1
        return True

    @classmethod
    def from_dict(cls, d: dict) -> FaultRule:
        if d["kind"] not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {d['kind']}")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class FaultProxy:
    upstreams: dict[str, str]
    http: httpx.AsyncClient
    rules: list[FaultRule] = field(default_factory=list)
    received: Counter = field(default_factory=Counter)  # 프록시가 받은 호출
    forwarded: Counter = field(default_factory=Counter)  # 업스트림까지 간 호출
    injected: Counter = field(default_factory=Counter)  # (agency, kind) 별 주입 횟수
    upstream_limit: str | None = None  # 실제 업스트림 한도 초과를 본 경우 그 내용
    limit_agency: str | None = None  # 그 기관
    limit_daily: bool = True  # 그날을 막는 한도인지(초당 한도면 False)

    def set_rules(self, rules: list[FaultRule]) -> None:
        self.rules = rules

    def reset_quota(self, agency: str | None = None) -> None:
        """한도 초기화(= 다음 날). quota 규칙을 지운다."""
        self.rules = [r for r in self.rules
                      if not (r.kind == "quota" and (agency is None or r.matches(agency)))]

    def stats(self) -> dict:
        return {"received": dict(self.received), "forwarded": dict(self.forwarded),
                "injected": {f"{a}:{k}": n for (a, k), n in self.injected.items()}}

    def _pick(self, agency: str) -> FaultRule | None:
        for rule in self.rules:
            if rule.matches(agency) and rule.take():
                return rule
        return None

    async def _forward(self, agency: str, path: str, request: Request) -> httpx.Response:
        self.forwarded[agency] += 1
        url = f"{self.upstreams[agency].rstrip('/')}/{path}"
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "accept", "authorization")}
        resp = await self.http.request(request.method, url, params=request.query_params,
                                       content=await request.body(), headers=headers)
        if self.upstream_limit is None and (hit := real_limit(agency, resp)):
            (self.upstream_limit, self.limit_daily), self.limit_agency = hit, agency
        return resp

    def _error_body(self, agency: str, path: str, rule: FaultRule, quota: bool) -> Response:
        if agency == "dart":
            code = "020" if quota else (rule.code or "900")
            if path.endswith(".xml"):
                xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><result>'
                       f"<status>{code}</status><message>{DART_MSG.get(code, '')}</message></result>")
                return Response(xml, status_code=rule.status, media_type="application/xml")
            return JSONResponse({"status": code, "message": DART_MSG.get(code, "")},
                                status_code=rule.status)
        code = "22" if quota else (rule.code or "01")
        xml = GATEWAY_XML.format(err=GATEWAY_ERR.get(code, "APPLICATION_ERROR"), code=code)
        return Response(xml, status_code=rule.status, media_type="application/xml")

    async def handle(self, request: Request) -> Response:
        agency = request.path_params["agency"]
        path = request.path_params["path"]
        if agency not in self.upstreams:
            return JSONResponse({"error": f"unknown agency {agency}"}, status_code=404)
        self.received[agency] += 1
        rule = self._pick(agency)
        if rule is None:
            return _relay(await self._forward(agency, path, request))
        self.injected[(agency, rule.kind)] += 1

        if rule.kind == "timeout":
            await asyncio.sleep(rule.delay_s)
            return _relay(await self._forward(agency, path, request))
        if rule.kind == "http_5xx":
            return Response("<html><body>503 Service Unavailable</body></html>",
                            status_code=503, media_type="text/html")
        if rule.kind in ("error_200", "quota"):
            return self._error_body(agency, path, rule, quota=rule.kind == "quota")

        upstream = await self._forward(agency, path, request)
        if rule.kind == "malformed":
            body = upstream.content
            return Response(body[: len(body) // 2], status_code=upstream.status_code,
                            media_type=upstream.headers.get("content-type"))
        # partial
        try:
            payload = upstream.json()
        except ValueError:
            return _relay(upstream)
        _drop_field(payload, PARTIAL_DROP[agency])
        return JSONResponse(payload, status_code=upstream.status_code)

    def app(self) -> Starlette:
        async def reset(request: Request) -> Response:
            self.reset_quota(request.query_params.get("agency"))
            return JSONResponse({"ok": True})

        async def stats(request: Request) -> Response:
            return JSONResponse(self.stats())

        return Starlette(routes=[
            Route("/__admin/reset_quota", reset, methods=["POST"]),
            Route("/__admin/stats", stats),
            Route("/{agency}/{path:path}", self.handle, methods=["GET", "POST"]),
        ])


def _relay(resp: httpx.Response) -> Response:
    return Response(resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type"))


def _drop_field(obj: object, name: str) -> None:
    if isinstance(obj, dict):
        obj.pop(name, None)
        for v in obj.values():
            _drop_field(v, name)
    elif isinstance(obj, list):
        for v in obj:
            _drop_field(v, name)


def load_rules(path: Path) -> list[FaultRule]:
    return [FaultRule.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def main() -> None:
    """프록시를 단독 서버로 띄운다: FAULT_PLAN=plan.json PROXY_PORT=8787."""
    import os

    import uvicorn

    from vendor_check.config import DART_DEFAULT_BASE, FSC_DEFAULT_BASE, NTS_DEFAULT_BASE

    proxy = FaultProxy(
        upstreams={"nts": NTS_DEFAULT_BASE, "fsc": FSC_DEFAULT_BASE, "dart": DART_DEFAULT_BASE},
        http=httpx.AsyncClient(timeout=60),
    )
    if os.environ.get("FAULT_PLAN"):
        proxy.set_rules(load_rules(Path(os.environ["FAULT_PLAN"])))
    uvicorn.run(proxy.app(), host="127.0.0.1", port=int(os.environ.get("PROXY_PORT", "8787")))


if __name__ == "__main__":
    main()
