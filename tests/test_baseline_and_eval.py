"""대조군 파이프라인, 정답 생성, 실험 러너, 지표 계산.

여기서 나오는 숫자는 합성 데이터에 대한 테스트 단언이며 프로젝트 결과가 아니다.
"""

from langgraph.checkpoint.memory import InMemorySaver

from tests.conftest import by_id
from vendor_check.eval.groundtruth import build_truth
from vendor_check.eval.metrics import field_outcomes, percentile, summarize
from vendor_check.eval.report import PENDING, render
from vendor_check.eval.run import run_matrix
from vendor_check.eval.scenarios import all_scenarios, select
from vendor_check.llm import StubReportWriter
from vendor_check.pipeline import run_baseline
from vendor_check.proxy import FaultRule
from vendor_check.recovery import FIELDS, RetryPolicy, Strategy, draft_report

FAST = RetryPolicy(max_attempts=3, base_delay_s=0)


def truth_of(c: dict) -> dict:
    fields = {"corp_name": c["corp_name"], "ceo_name": c["ceo_name"], "crno": c["crno"],
              "bzno": c["bzno"], "market": c["market"], "b_stt_cd": c["b_stt_cd"],
              "tax_type_cd": c["tax_type_cd"]}
    return {"id": c["id"], "corp_name": c["corp_name"], "complete": True, "fields": fields}


async def test_baseline_clean_run(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE)
    assert out["status"] == "done"
    assert all(e["status"] == "ok" for e in out["report"].values())
    assert [x["tool"] for x in out["calls"]] == [
        "search_corp", "business_status", "dart_find_corp", "dart_company"]


async def test_baseline_picks_first_same_name_candidate(make_stack, companies):
    c = by_id(companies, "syn-02b")
    async with make_stack() as stack:
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE)
    assert out["report"]["crno"]["value"] == "0000000000021"  # 사람 확인이 없어 다른 법인


async def test_baseline_checkpoint_strategy_stops_on_quota(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("dart", "quota")])
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.CHECKPOINT)
    assert out["status"] == "interrupted"


async def test_lenient_detection_turns_error_body_into_wrong_normal_value(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack("lenient") as stack:
        stack.proxy.set_rules([FaultRule("nts", "error_200")])
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.RETRY)
    # 200 + XML 오류를 "결과 없음"으로 읽어 미등록이라고 정상 보고한다
    assert out["report"]["b_stt_cd"] == {"value": "unregistered", "status": "ok", "source": "nts"}
    outcomes = {f: (ok, correct) for f, ok, correct in field_outcomes(out, truth_of(c)["fields"])}
    assert outcomes["b_stt_cd"] == (True, False)


def test_draft_report_uses_secondary_only_under_fallback():
    sources = {"fsc": None, "dart": {"corp_name": "D", "market": "KOSPI"}, "nts": None}
    none = draft_report(sources, Strategy.NONE)
    fb = draft_report(sources, Strategy.FALLBACK)
    assert none["corp_name"]["status"] == "unknown"
    assert fb["corp_name"] == {"value": "D", "status": "ok", "source": "dart"}
    assert set(fb) == set(FIELDS)


async def test_groundtruth_uses_bzno_to_pick_entity(make_stack, companies):
    c = by_id(companies, "syn-02b")
    async with make_stack() as stack:
        rec = await build_truth(stack.gateway, {"id": c["id"], "corp_name": c["corp_name"],
                                                "bzno": "000-00-00022"})
    assert rec["complete"] is True
    assert rec["fields"] == truth_of(c)["fields"]
    assert rec["n_candidates"] == 2


async def test_groundtruth_marks_incomplete_on_failure(make_stack, companies):
    c = by_id(companies, "syn-01")
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("nts", "http_5xx")])
        rec = await build_truth(stack.gateway, {"id": c["id"], "corp_name": c["corp_name"],
                                                "bzno": c["bzno"]})
    assert rec["complete"] is False and "nts" in rec["reason"]


async def test_run_matrix_records_and_resume(make_stack, companies):
    truths = [truth_of(by_id(companies, i)) for i in ("syn-01", "syn-02b")]
    scenarios = select(["clean", "nts-quota"])
    async with make_stack() as stack:
        records = await run_matrix(stack, truths, ["baseline", "agent"],
                                   [Strategy.NONE, Strategy.CHECKPOINT], scenarios,
                                   StubReportWriter(), InMemorySaver(), policy=FAST)
    assert len(records) == 2 * 2 * 2 * 2
    quota_ckpt = [r for r in records if r["scenario"] == "nts-quota" and r["strategy"] == "checkpoint"]
    assert all(r["interrupted"] and r["status"] == "done" for r in quota_ckpt)
    agent_02b = next(r for r in records if r["system"] == "agent" and r["company_id"] == "syn-02b"
                     and r["scenario"] == "clean" and r["strategy"] == "none")
    assert agent_02b["hitl"] == 1
    summary = summarize(records, {t["id"]: t for t in truths})
    keys = {(g["system"], g["strategy"], g["fault_kind"]) for g in summary["groups"]}
    assert ("agent", "checkpoint", "quota") in keys
    agent_ckpt = next(g for g in summary["groups"]
                      if g["system"] == "agent" and g["strategy"] == "checkpoint"
                      and g["fault_kind"] == "quota")
    assert agent_ckpt["interrupted"] == 2 and agent_ckpt["resume_success"] == 1.0


def test_summarize_counts_silent_wrong_values():
    truth = {"x": {"fields": {f: "v" for f in FIELDS}}}
    report = {f: {"value": "v", "status": "ok", "source": "fsc"} for f in FIELDS}
    report["ceo_name"] = {"value": "wrong", "status": "ok", "source": "fsc"}
    report["bzno"] = {"value": None, "status": "unknown", "source": None}
    rec = {"company_id": "x", "system": "agent", "strategy": "none", "scenario_kind": None,
           "scenario_mode": None, "status": "done", "report": report, "elapsed_s": 1.0,
           "input_tokens": 100, "output_tokens": 20}
    s = summarize([rec], truth, price_in=1.0, price_out=2.0)
    g = s["groups"][0]
    assert g["silent_wrong_rate"] == 1 / 6
    assert g["item_agreement"] == 5 / 7
    assert g["check_success"] == 0
    assert g["cost_per_check"] == (100 * 1.0 + 20 * 2.0) / 1e6


def test_percentile_and_scenarios():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([], 95) is None
    names = [s.name for s in all_scenarios()]
    assert names[0] == "clean" and "fsc-error_200-transient" in names and "dart-quota" in names
    assert len(names) == 1 + 3 * (5 * 2 + 1)


def test_render_without_measurements_has_empty_table():
    md = render(None)
    assert PENDING in md
    assert "%" not in md
