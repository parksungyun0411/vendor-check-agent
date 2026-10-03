"""LangGraph 에이전트.

resolve ─┬─(후보 0)──────────────────────────────▶ report
         ├─(동명 법인 2+)─▶ confirm(HITL interrupt) ─┐
         └─(후보 1)──────────────────────────────────┤
                                                     ▼
                                 fan-out(Send): nts ∥ dart(상장·DART 등록 법인만) ─▶ report(LLM)

- 동명 법인: confirm 노드가 interrupt 로 멈추고 사람이 법인등록번호를 고른다.
- 분기: 금융위 결과가 비상장이고 DART 고유번호(fssCorpUnqNo)도 없으면 DART 를 부르지 않는다.
- 병렬: 국세청과 DART 조회는 Send 로 같은 superstep 에서 돈다.
- 재개: checkpoint 전략에서 일일 한도 초과를 만나면 해당 노드가 interrupt 로 멈춘다. 체크포인터에
  상태가 남아 있어, 다음 날 Command(resume=...) 로 이어 가면 이미 끝난 기관은 다시 부르지 않는다.
"""

from __future__ import annotations

import operator
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from vendor_check.gateway import ToolGateway
from vendor_check.llm import ReportWriter
from vendor_check.pipeline import QuotaHit, Steps, fsc_values
from vendor_check.recovery import RetryPolicy, Strategy, draft_report

LISTED = {"KOSPI", "KOSDAQ", "KONEX"}


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class CheckState(TypedDict, total=False):
    query: dict
    candidates: list[dict]
    candidate_source: str
    entity: dict | None
    sources: Annotated[dict, _merge]
    calls: Annotated[list, operator.add]
    hitl: Annotated[int, operator.add]
    report: dict
    summary: str
    usage: Annotated[list, operator.add]


async def _guarded(make: Callable[[], Awaitable[Any]]) -> Any:
    """한도 초과면 interrupt 로 멈춘다. 재개되면 같은 호출을 다시 한다."""
    while True:
        try:
            return await make()
        except QuotaHit as e:
            interrupt({"type": "quota", "tool": e.tool})


def needs_dart(entity: dict) -> bool:
    return entity.get("market") in LISTED or bool(entity.get("fss_corp_code"))


def build_graph(gateway: ToolGateway, writer: ReportWriter, strategy: Strategy,
                policy: RetryPolicy | None = None, checkpointer: Any = None):
    def steps() -> Steps:
        return Steps(gateway, strategy, policy)

    async def resolve(state: CheckState) -> dict:
        s = steps()
        name = state["query"]["corp_name"]
        fsc_cands, dart_cands = await _guarded(lambda: s.search(name))
        out: dict = {"calls": s.calls}
        if fsc_cands is not None:
            entity = fsc_cands[0] if len(fsc_cands) == 1 else None
            out.update(candidates=fsc_cands, candidate_source="fsc", entity=entity)
            if len(fsc_cands) <= 1:
                out["sources"] = {"fsc": fsc_values(True, entity)}
        elif dart_cands:
            # 금융위 실패 → fallback 전략이 DART 고유번호 목록에서 후보를 찾았다
            entity = dart_cands[0] if len(dart_cands) == 1 else None
            out.update(candidates=dart_cands, candidate_source="dart", entity=entity)
            out["sources"] = {"fsc": None, "dart": entity} if entity else {"fsc": None}
        else:
            out["candidates"] = []
            out["entity"] = None
            out["sources"] = {"fsc": None}
        return out

    async def confirm(state: CheckState) -> dict:
        cands = state["candidates"]
        crno = interrupt({
            "type": "confirm_entity",
            "query": state["query"],
            "candidates": [{k: c.get(k) for k in ("crno", "bzno", "corp_name", "ceo_name", "market")}
                           for c in cands],
        })
        entity = next((c for c in cands if c.get("crno") == crno), None)
        if state.get("candidate_source") == "dart":
            sources = {"dart": entity}
        else:
            sources = {"fsc": fsc_values(True, entity)}
        return {"entity": entity, "hitl": 1, "sources": sources}

    def after_resolve(state: CheckState):
        cands = state.get("candidates") or []
        if state.get("entity") is None and len(cands) > 1:
            return "confirm"
        return fan_out(state)

    def fan_out(state: CheckState):
        entity = state.get("entity")
        if entity is None:
            return "report"
        sends = []
        if entity.get("bzno"):
            sends.append(Send("nts", {"entity": entity}))
        if "dart" not in state.get("sources", {}) and needs_dart(entity):
            sends.append(Send("dart", {"entity": entity, "query": state["query"]}))
        return sends or "report"

    async def nts(state: CheckState) -> dict:
        s = steps()
        values = await _guarded(lambda: s.nts(state["entity"]["bzno"]))
        return {"sources": {"nts": values}, "calls": s.calls}

    async def dart(state: CheckState) -> dict:
        s = steps()
        entity = state["entity"]
        name = entity.get("corp_name") or state["query"]["corp_name"]
        values = await _guarded(lambda: s.dart(name, match_crno=entity.get("crno")))
        return {"sources": {"dart": values}, "calls": s.calls}

    async def report(state: CheckState) -> dict:
        sources = dict(state.get("sources", {}))
        entity = state.get("entity")
        if entity is not None and "dart" not in sources and not needs_dart(entity):
            # 분기로 DART 를 건너뛴 비상장 법인: 상장 구분은 금융위 값으로 확정한다.
            sources["dart"] = {"market": entity.get("market") or "NONE"}
        draft = draft_report(sources, strategy)
        failures = [c for c in state.get("calls", []) if not c["ok"]]
        final, summary, usage = await writer.write(state["query"], draft, failures)
        return {"report": final, "summary": summary,
                "usage": [{"input_tokens": usage.input_tokens,
                           "output_tokens": usage.output_tokens, "model": usage.model}]}

    g = StateGraph(CheckState)
    g.add_node("resolve", resolve)
    g.add_node("confirm", confirm)
    g.add_node("nts", nts)
    g.add_node("dart", dart)
    g.add_node("report", report)
    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", after_resolve, ["confirm", "nts", "dart", "report"])
    g.add_conditional_edges("confirm", fan_out, ["nts", "dart", "report"])
    g.add_edge("nts", "report")
    g.add_edge("dart", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


Approver = Callable[[dict, list[dict]], str]


async def drive(graph, thread_id: str, approver: Approver, query: dict | None = None,
                resume_quota: bool = False) -> dict:
    """그래프를 끝까지 또는 한도 초과 interrupt 까지 돌린다.

    query 를 주면 새로 시작, resume_quota=True 면 한도 초과로 멈춘 스레드를 이어 간다.
    동명 법인 확인 interrupt 는 approver 로 답한다.
    """
    config = {"configurable": {"thread_id": thread_id}}
    started = time.perf_counter()
    if query is not None:
        await graph.ainvoke({"query": query}, config)
    elif resume_quota:
        snap = await graph.aget_state(config)
        await graph.ainvoke(Command(resume={i.id: "quota_reset" for i in snap.interrupts}), config)
    while True:
        snap = await graph.aget_state(config)
        if not snap.interrupts:
            values = snap.values
            return {"status": "done", "report": values.get("report"),
                    "summary": values.get("summary"), "entity": values.get("entity"),
                    "calls": values.get("calls", []), "usage": values.get("usage", []),
                    "hitl": values.get("hitl", 0), "elapsed_s": time.perf_counter() - started}
        pending = [i for i in snap.interrupts if i.value["type"] == "confirm_entity"]
        if not pending:
            return {"status": "interrupted", "interrupt": snap.interrupts[0].value,
                    "calls": snap.values.get("calls", []),
                    "elapsed_s": time.perf_counter() - started}
        answers = {i.id: approver(i.value["query"], i.value["candidates"]) for i in pending}
        await graph.ainvoke(Command(resume=answers), config)
