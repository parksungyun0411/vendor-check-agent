"""기관별 툴. MCP 서버가 이 함수들을 그대로 노출한다.

모든 툴은 예외를 던지지 않고 {"ok": True, "data": ...} 또는 {"ok": False, "error": {...}}
를 돌려준다. 실패 분류를 MCP 경계 너머까지 보존하기 위해서다.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from vendor_check.clients.dart import DartClient
from vendor_check.clients.fsc import FscClient
from vendor_check.clients.nts import NtsClient, normalize_bno
from vendor_check.config import Settings
from vendor_check.errors import ToolFailure

TOOL_NAMES = ("search_corp", "fsc_outline", "business_status", "dart_find_corp", "dart_company")
LEGAL_FORM = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|\s+")


def corp_key(name: str | None) -> str:
    """법인 형태 표기와 공백을 뗀 비교용 상호. 금융위는 '(주)가나', DART 목록은 '가나'처럼 적는다."""
    return LEGAL_FORM.sub("", name or "")


async def _wrap(fn: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": await fn()}
    except ToolFailure as e:
        return {"ok": False, "error": e.error.to_dict()}


class AgencyTools:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        strict = settings.detection != "lenient"
        common = {"http": http, "timeout_s": settings.timeout_s, "strict": strict}
        self.settings = settings
        self.nts = NtsClient(settings.nts_base, settings.data_go_kr_key, **common)
        self.fsc = FscClient(settings.fsc_base, settings.data_go_kr_key, **common)
        self.dart = DartClient(settings.dart_base, settings.dart_key, **common)
        self._corpcodes: list[dict] | None = None
        self._by_key: dict[str, list[dict]] | None = None

    async def _corp_index(self) -> list[dict]:
        if self._corpcodes is None:
            self._corpcodes = await self.dart.download_corpcodes(self.settings.corpcode_cache)
        return self._corpcodes

    async def search_corp(self, corp_name: str) -> dict[str, Any]:
        """상호로 금융위 기업개요를 검색해 후보 법인 목록을 돌려준다."""
        return await _wrap(lambda: self.fsc.corp_outline(corp_name=corp_name))

    async def fsc_outline(self, crno: str) -> dict[str, Any]:
        """법인등록번호로 금융위 기업개요 한 건을 조회한다."""
        async def run():
            rows = await self.fsc.corp_outline(crno=crno)
            return rows[0] if rows else None
        return await _wrap(run)

    async def business_status(self, bzno: str) -> dict[str, Any]:
        """사업자등록번호로 국세청 납세자 상태(계속/휴업/폐업)를 조회한다."""
        async def run():
            b = normalize_bno(bzno)
            return (await self.nts.status([b]))[b]
        return await _wrap(run)

    async def dart_find_corp(self, corp_name: str) -> dict[str, Any]:
        """OpenDART 고유번호 목록에서 회사명이 같은 항목을 찾는다. (주)·주식회사 표기와 공백은 무시한다."""
        async def run():
            if self._by_key is None:
                index: dict[str, list[dict]] = {}
                for c in await self._corp_index():
                    index.setdefault(corp_key(c["corp_name"]), []).append(c)
                self._by_key = index
            key = corp_key(corp_name)
            return list(self._by_key.get(key, [])) if key else []
        return await _wrap(run)

    async def dart_company(self, corp_code: str) -> dict[str, Any]:
        """OpenDART 기업개황을 조회한다. 조회 결과가 없으면 data는 null이다."""
        return await _wrap(lambda: self.dart.company(corp_code))

    def as_mapping(self) -> dict[str, Callable[..., Awaitable[dict]]]:
        return {name: getattr(self, name) for name in TOOL_NAMES}
