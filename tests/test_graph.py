"""그래프를 실제로 돌려 분기·병렬·HITL·체크포인트 재개·복구 경로를 확인한다."""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from tests.conftest import by_id
from vendor_check.graph import build_graph, drive
from vendor_check.llm import StubReportWriter
from vendor_check.proxy import FaultRule
from vendor_check.recovery import RetryPolicy, Strategy

FAST = RetryPolicy(max_attempts=3, base_delay_s=0)


def values(report: dict) -> dict:
    return {f: e["value"] for f, e in report.items() if e["status"] == "ok"}


def never(query, candidates):
    raise AssertionError("HITL should not be asked")


def pick(crno):
    asked = []

    def approve(query, candidates):
        asked.append([c["crno"] for c in candidates])
        return crno

    approve.asked = asked
    return approve


async def test_single_listed_company_queries_nts_and_dart(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t1", never, query={"corp_name": c["corp_name"]})
        received = stack.proxy.stats()["received"]
    assert out["status"] == "done"
    assert values(out["report"]) == {
        "corp_name": c["corp_name"], "ceo_name": c["ceo_name"], "crno": c["crno"],
        "bzno": c["bzno"], "market": "KOSPI", "b_stt_cd": "01", "tax_type_cd": "01"}
    assert received["nts"] == 1 and received["dart"] >= 1
    assert {call["tool"] for call in out["calls"]} >= {"business_status", "dart_company"}


async def test_listed_company_found_in_dart_despite_legal_form_in_fsc_name(make_stack, companies):
    c = by_id(companies, "syn-04")  # 금융위 '(주)가상상장전자', DART 목록 '가상상장전자'
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t1b", never, query={"corp_name": c["corp_name"]})
        received = stack.proxy.stats()["received"]
    assert received["dart"] >= 1
    assert out["report"]["market"] == {"value": "KOSDAQ", "status": "ok", "source": "dart"}


async def test_dart_looked_up_by_fss_code_when_names_differ(make_stack, companies):
    c = by_id(companies, "syn-05")  # 금융위 '가상단조공업(주)', DART 목록 '가상단조'. 이름으로는 못 찾는다
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t1c", never, query={"corp_name": c["corp_name"]})
    assert out["report"]["market"] == {"value": "KOSDAQ", "status": "ok", "source": "dart"}
    assert "dart_find_corp" not in {call["tool"] for call in out["calls"]}


async def test_unlisted_company_without_dart_code_skips_dart(make_stack, companies):
    c = by_id(companies, "syn-03")
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t2", never, query={"corp_name": c["corp_name"]})
        received = stack.proxy.stats()["received"]
    assert "dart" not in received  # 분기로 DART 를 부르지 않았다
    assert out["report"]["market"] == {"value": "NONE", "status": "ok", "source": "dart"}
    assert out["report"]["b_stt_cd"]["value"] == "03"


async def test_same_name_companies_interrupt_for_human_choice(make_stack, companies):
    target = by_id(companies, "syn-02b")
    approve = pick(target["crno"])
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t3", approve, query={"corp_name": target["corp_name"]})
    assert approve.asked == [["0000000000021", "0000000000022"]]
    assert out["hitl"] == 1
    assert out["report"]["crno"]["value"] == target["crno"]
    assert out["report"]["market"]["value"] == "KOSDAQ"


async def test_unknown_name_goes_straight_to_report(make_stack):
    async with make_stack() as stack:
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t4", never, query={"corp_name": "존재하지않는가상법인"})
        received = stack.proxy.stats()["received"]
    assert received == {"fsc": 1}
    assert out["report"]["corp_name"] == {"value": None, "status": "ok", "source": "fsc"}
    assert out["report"]["b_stt_cd"]["status"] == "unknown"


async def test_quota_interrupt_then_resume_does_not_recall_finished_agency(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("nts", "quota")])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.CHECKPOINT, FAST,
                            InMemorySaver())
        first = await drive(graph, "t5", never, query={"corp_name": c["corp_name"]})
        before = stack.proxy.stats()["forwarded"]
        stack.proxy.reset_quota()  # 다음 날
        second = await drive(graph, "t5", never, resume_quota=True)
        after = stack.proxy.stats()["forwarded"]
    assert first["status"] == "interrupted"
    assert first["interrupt"] == {"type": "quota", "tool": "business_status"}
    assert second["status"] == "done"
    assert values(second["report"])["b_stt_cd"] == "01"
    assert after["dart"] == before["dart"]  # 병렬로 끝난 DART 는 다시 부르지 않았다
    assert after["fsc"] == before["fsc"]
    assert after["nts"] == before.get("nts", 0) + 1


async def test_quota_still_exhausted_pauses_again(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("fsc", "quota")])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.CHECKPOINT, FAST,
                            InMemorySaver())
        await drive(graph, "t6", never, query={"corp_name": c["corp_name"]})
        again = await drive(graph, "t6", never, resume_quota=True)  # 한도가 아직 안 풀림
        stack.proxy.reset_quota()
        done = await drive(graph, "t6", never, resume_quota=True)
    assert again["status"] == "interrupted"
    assert done["status"] == "done"


async def test_resume_from_sqlite_with_a_fresh_graph(make_stack, companies, tmp_path):
    """프로세스가 바뀐 상황: 같은 DB 를 연 새 그래프 인스턴스로 이어 간다."""
    c = by_id(companies, "syn-01")
    db = str(tmp_path / "cp.sqlite")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("dart", "quota")])
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            g1 = build_graph(stack.gateway, StubReportWriter(), Strategy.CHECKPOINT, FAST, saver)
            first = await drive(g1, "t7", never, query={"corp_name": c["corp_name"]})
        stack.proxy.reset_quota()
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            g2 = build_graph(stack.gateway, StubReportWriter(), Strategy.CHECKPOINT, FAST, saver)
            second = await drive(g2, "t7", never, resume_quota=True)
    assert first["status"] == "interrupted"
    assert second["status"] == "done"
    assert values(second["report"])["market"] == "KOSPI"


async def test_quota_without_checkpoint_strategy_reports_unknown(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("nts", "quota")])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.RETRY, FAST, InMemorySaver())
        out = await drive(graph, "t8", never, query={"corp_name": c["corp_name"]})
        received = stack.proxy.stats()["received"]
    assert out["status"] == "done"
    assert out["report"]["b_stt_cd"]["status"] == "unknown"
    assert received["nts"] == 1  # 한도 초과는 재시도하지 않는다


async def test_retry_recovers_transient_failure(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("nts", "error_200", times=2)])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.RETRY, FAST, InMemorySaver())
        out = await drive(graph, "t9", never, query={"corp_name": c["corp_name"]})
    assert out["report"]["b_stt_cd"] == {"value": "01", "status": "ok", "source": "nts"}
    nts_call = next(x for x in out["calls"] if x["tool"] == "business_status")
    assert nts_call["attempts"] == 3


async def test_fallback_resolves_through_dart_when_fsc_is_down(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("fsc", "http_5xx")])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.FALLBACK, FAST,
                            InMemorySaver())
        out = await drive(graph, "t10", never, query={"corp_name": c["corp_name"]})
    report = out["report"]
    assert {report[f]["source"] for f in ("corp_name", "ceo_name", "crno", "bzno")} == {"dart"}
    assert values(report)["crno"] == c["crno"]
    assert report["b_stt_cd"]["value"] == "01"  # DART 의 사업자번호로 국세청까지 이어졌다


async def test_none_strategy_leaves_failed_fields_unknown(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("fsc", "http_5xx")])
        graph = build_graph(stack.gateway, StubReportWriter(), Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t11", never, query={"corp_name": c["corp_name"]})
    assert all(e["status"] == "unknown" for e in out["report"].values())


async def test_report_writer_receives_failures_and_usage_is_recorded(make_stack, companies):
    c = by_id(companies, "syn-01")
    writer = StubReportWriter()
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("dart", "http_5xx")])
        graph = build_graph(stack.gateway, writer, Strategy.NONE, FAST, InMemorySaver())
        out = await drive(graph, "t12", never, query={"corp_name": c["corp_name"]})
    assert writer.calls == 1
    assert out["usage"] == [{"input_tokens": 0, "output_tokens": 0, "model": "stub"}]
    assert out["report"]["market"]["status"] == "unknown"
