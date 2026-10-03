"""대조군: 고정 순서 파이프라인 (금융위 검색 → 국세청 → OpenDART).

동명 법인이 여러 개면 첫 후보를 고른다(사람 확인 없음). 상장 여부와 관계없이 세 기관을 모두
부른다. 에이전트와 같은 툴·복구 전략·병합 함수를 써서, 차이가 그래프 구조에서만 나오게 했다.
"""

from __future__ import annotations

from vendor_check.gateway import ToolGateway
from vendor_check.recovery import (
    RetryPolicy,
    Strategy,
    call_tool,
    draft_report,
    is_quota,
    nts_values,
)

CORP_FIELDS = ("corp_name", "ceo_name", "crno", "bzno", "market")


class QuotaHit(Exception):
    """checkpoint 전략에서 일일 한도 초과를 만나 멈춰야 할 때."""

    def __init__(self, tool: str):
        super().__init__(tool)
        self.tool = tool


class Steps:
    """대조군과 에이전트 노드가 공유하는 조회 단계. 호출 기록을 self.calls 에 쌓는다."""

    def __init__(self, gateway: ToolGateway, strategy: Strategy, policy: RetryPolicy | None = None):
        self.gateway = gateway
        self.strategy = strategy
        self.policy = policy or RetryPolicy()
        self.calls: list[dict] = []

    async def call(self, name: str, args: dict) -> dict:
        result, log = await call_tool(self.gateway, name, args, self.strategy, self.policy)
        self.calls.append(log)
        if self.strategy == Strategy.CHECKPOINT and is_quota(result):
            raise QuotaHit(name)
        return result

    async def search(self, corp_name: str) -> tuple[list[dict] | None, list[dict] | None]:
        """(금융위 후보, DART 대체 후보). 금융위가 실패하면 첫 값은 None."""
        r = await self.call("search_corp", {"corp_name": corp_name})
        if r["ok"]:
            return r["data"], None
        if self.strategy != Strategy.FALLBACK:
            return None, None
        return None, await self.dart_candidates(corp_name)

    async def dart_candidates(self, corp_name: str) -> list[dict] | None:
        found = await self.call("dart_find_corp", {"corp_name": corp_name})
        if not found["ok"]:
            return None
        out = []
        for c in found["data"]:
            r = await self.call("dart_company", {"corp_code": c["corp_code"]})
            if r["ok"] and r["data"]:
                out.append(r["data"])
        return out

    async def nts(self, bzno: str | None) -> dict | None:
        if not bzno:
            return None
        r = await self.call("business_status", {"bzno": bzno})
        return nts_values(r["data"]) if r["ok"] else None

    async def dart(self, corp_name: str, match_crno: str | None = None) -> dict | None:
        """DART 값. 고유번호 목록에 없으면 비상장으로 보고 market=NONE."""
        found = await self.call("dart_find_corp", {"corp_name": corp_name})
        if not found["ok"]:
            return None
        if not found["data"]:
            return {"market": "NONE"}
        for c in found["data"]:
            r = await self.call("dart_company", {"corp_code": c["corp_code"]})
            if not r["ok"]:
                return None
            if r["data"] is None:
                continue
            if match_crno is None or r["data"].get("crno") == match_crno:
                return r["data"]
        return {"market": "NONE"}


def fsc_values(search_ok: bool, entity: dict | None) -> dict | None:
    if not search_ok:
        return None
    if entity is None:
        # 검색은 성공했는데 후보가 없다: "해당 법인 없음"을 정상 결과로 보고한다.
        return {f: None for f in CORP_FIELDS}
    return {f: entity.get(f) for f in CORP_FIELDS}


async def run_baseline(gateway: ToolGateway, query: dict, strategy: Strategy,
                       policy: RetryPolicy | None = None) -> dict:
    steps = Steps(gateway, strategy, policy)
    sources: dict[str, dict | None] = {}
    entity = None
    try:
        fsc_cands, dart_cands = await steps.search(query["corp_name"])
        if fsc_cands is not None:
            entity = fsc_cands[0] if fsc_cands else None
            sources["fsc"] = fsc_values(True, entity)
        elif dart_cands:
            entity = dart_cands[0]
            sources["fsc"] = None
            sources["dart"] = entity
        else:
            sources["fsc"] = None
        sources["nts"] = await steps.nts(entity.get("bzno") if entity else None)
        if "dart" not in sources:
            sources["dart"] = await steps.dart(query["corp_name"])
    except QuotaHit as e:
        return {"status": "interrupted", "interrupt": {"type": "quota", "tool": e.tool},
                "report": None, "calls": steps.calls, "entity": entity}
    return {"status": "done", "report": draft_report(sources, strategy),
            "calls": steps.calls, "entity": entity}
