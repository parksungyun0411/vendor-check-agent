"""대조군 파이프라인, 정답 생성, 실험 러너, 지표 계산.

여기서 나오는 숫자는 합성 데이터에 대한 테스트 단언이며 프로젝트 결과가 아니다.
"""

from langgraph.checkpoint.memory import InMemorySaver

from tests.conftest import by_id
from vendor_check.eval.groundtruth import build_truth
from vendor_check.eval.metrics import field_outcomes, percentile, summarize
from vendor_check.eval.report import PENDING, render
from vendor_check.eval.run import approver_for, run_matrix
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


async def test_baseline_with_truth_approver_confirms_same_name_entity(make_stack, companies):
    c = by_id(companies, "syn-02b")
    async with make_stack() as stack:
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE,
                                 approver=approver_for(truth_of(c)))
    assert out["hitl"] == 1
    assert all(correct for _, _, correct in field_outcomes(out, truth_of(c)["fields"]))


async def test_baseline_confirm_matches_dart_record_to_chosen_entity(make_stack, companies):
    c = by_id(companies, "syn-02a")  # 비상장. DART 에는 동명의 syn-02b 만 있다
    query = {"corp_name": c["corp_name"]}
    async with make_stack() as stack:
        first = await run_baseline(stack.gateway, query, Strategy.NONE)
        confirmed = await run_baseline(stack.gateway, query, Strategy.NONE,
                                       approver=approver_for(truth_of(c)))
    # 첫 후보를 쓰면 DART 를 이름으로만 찾아 동명 상장사의 상장 구분을 가져온다
    assert first["report"]["market"]["value"] == "KOSDAQ"
    assert confirmed["report"]["market"]["value"] == "NONE"
    assert all(correct for _, _, correct in field_outcomes(confirmed, truth_of(c)["fields"]))


async def test_baseline_confirm_looks_up_dart_like_agent(make_stack, companies, monkeypatch):
    from vendor_check import pipeline

    seen = []
    original = pipeline.Steps.dart

    async def spy(self, corp_name, match_crno=None):
        seen.append((corp_name, match_crno))
        return await original(self, corp_name, match_crno)

    monkeypatch.setattr(pipeline.Steps, "dart", spy)
    c = by_id(companies, "syn-01")  # 동명 법인이 아니어도 확인 조건이면 법인등록번호로 맞춘다
    async with make_stack() as stack:
        await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE)
        await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE,
                           approver=approver_for(truth_of(c)))
    assert seen == [(c["corp_name"], None), (c["corp_name"], c["crno"])]


async def test_baseline_approver_not_asked_for_single_candidate(make_stack, companies):
    c = by_id(companies, "syn-01")

    def never(query, candidates):
        raise AssertionError("동명 법인이 아니면 확인하지 않는다")

    async with make_stack() as stack:
        out = await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE,
                                 approver=never)
    assert out["hitl"] == 0 and out["status"] == "done"


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


async def test_run_matrix_baseline_confirm_is_counted_separately(make_stack, companies):
    truths = [truth_of(by_id(companies, i)) for i in ("syn-01", "syn-02a", "syn-02b")]
    scenarios = select(["clean"])
    async with make_stack() as stack:
        first = await run_matrix(stack, truths, ["baseline"], [Strategy.NONE], scenarios,
                                 StubReportWriter(), InMemorySaver(), policy=FAST)
        confirm = await run_matrix(stack, truths, ["baseline"], [Strategy.NONE], scenarios,
                                   StubReportWriter(), InMemorySaver(), policy=FAST,
                                   baseline_approver="truth")
        agent = await run_matrix(stack, truths, ["agent"], [Strategy.NONE], scenarios,
                                 StubReportWriter(), InMemorySaver(), policy=FAST)
    assert {r["approver"] for r in first} == {"first"}
    assert {r["approver"] for r in confirm + agent} == {"truth"}
    assert [r["hitl"] for r in confirm] == [0, 1, 1]
    overall = summarize(first + confirm + agent, {t["id"]: t for t in truths})["overall"]
    assert set(overall) == {"baseline", "baseline+confirm", "agent"}
    # 같은 확인 단계를 주면 무주입 합성 데이터에서는 대조군도 에이전트와 같아진다
    assert overall["baseline+confirm"] == overall["agent"]
    assert overall["baseline"]["item_agreement"] < overall["baseline+confirm"]["item_agreement"]


async def test_baseline_confirm_counts_questions_across_quota_restart(make_stack, companies):
    truths = [truth_of(by_id(companies, "syn-02b"))]
    async with make_stack() as stack:
        records = await run_matrix(stack, truths, ["baseline"], [Strategy.CHECKPOINT],
                                   select(["nts-quota"]), StubReportWriter(), InMemorySaver(),
                                   policy=FAST, baseline_approver="truth")
    assert records[0]["interrupted"] and records[0]["status"] == "done"
    assert records[0]["hitl"] == 2  # 처음부터 다시 돌아 동명 법인 확인을 두 번 묻는다


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
