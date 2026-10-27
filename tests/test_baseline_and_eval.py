"""대조군 파이프라인, 정답 생성, 실험 러너, 지표 계산.

여기서 나오는 숫자는 합성 데이터에 대한 테스트 단언이며 프로젝트 결과가 아니다.
"""

import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tests.conftest import by_id
from vendor_check.eval.groundtruth import build_truth
from vendor_check.eval.metrics import field_outcomes, percentile, summarize
from vendor_check.eval.report import PENDING, compute, render
from vendor_check.eval.run import (
    CallLedger,
    UpstreamLimit,
    approver_for,
    done_keys,
    parse_budget,
    run_matrix,
)
from vendor_check.eval.scenarios import all_scenarios, select
from vendor_check.llm import StubReportWriter
from vendor_check.pipeline import run_baseline
from vendor_check.proxy import FaultRule, real_limit
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

    async def spy(self, corp_name, match_crno=None, corp_code=None):
        seen.append((corp_name, match_crno, corp_code))
        return await original(self, corp_name, match_crno, corp_code)

    monkeypatch.setattr(pipeline.Steps, "dart", spy)
    c = by_id(companies, "syn-01")  # 동명 법인이 아니어도 확인 조건이면 법인등록번호로 맞춘다
    async with make_stack() as stack:
        await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE)
        await run_baseline(stack.gateway, {"corp_name": c["corp_name"]}, Strategy.NONE,
                           approver=approver_for(truth_of(c)))
    assert seen == [(c["corp_name"], None, None), (c["corp_name"], c["crno"], c["corp_code"])]


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


async def test_groundtruth_finds_dart_record_despite_legal_form(make_stack, companies):
    c = by_id(companies, "syn-04")  # 금융위 '(주)가상상장전자', DART 목록 '가상상장전자'
    async with make_stack() as stack:
        rec = await build_truth(stack.gateway, {"id": c["id"], "corp_name": c["corp_name"],
                                                "bzno": c["bzno"]})
    assert rec["complete"] is True
    assert rec["fields"]["corp_name"] == c["fsc_name"]
    assert rec["fields"]["market"] == "KOSDAQ"


async def test_groundtruth_uses_fss_code_when_names_differ(make_stack, companies):
    c = by_id(companies, "syn-05")
    async with make_stack() as stack:
        rec = await build_truth(stack.gateway, {"id": c["id"], "corp_name": c["corp_name"],
                                                "bzno": c["bzno"]})
    assert rec["complete"] is True and rec["fields"]["market"] == "KOSDAQ"


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


async def test_run_matrix_skips_recorded_runs_on_rerun(make_stack, companies, tmp_path):
    truths = [truth_of(by_id(companies, i)) for i in ("syn-01", "syn-02b")]
    out = tmp_path / "runs.jsonl"
    async with make_stack() as stack:
        first = await run_matrix(stack, truths, ["baseline", "agent"], [Strategy.NONE],
                                 select(["clean"]), StubReportWriter(), InMemorySaver(), out, FAST)
        again = await run_matrix(stack, truths, ["baseline", "agent"], [Strategy.NONE],
                                 select(["clean"]), StubReportWriter(), InMemorySaver(), out, FAST)
        confirm = await run_matrix(stack, truths, ["baseline"], [Strategy.NONE], select(["clean"]),
                                   StubReportWriter(), InMemorySaver(), out, FAST,
                                   baseline_approver="truth")
    assert len(first) == 4 and again == [] and len(confirm) == 2
    assert len(out.read_text(encoding="utf-8").splitlines()) == 6


def test_done_keys_drops_half_written_last_line(tmp_path):
    out = tmp_path / "runs.jsonl"
    rec = {"system": "agent", "strategy": "none", "scenario": "clean", "company_id": "syn-01",
           "approver": "truth"}
    out.write_text(json.dumps(rec) + "\n" + '{"system": "agent", "strat', encoding="utf-8")
    assert done_keys(out) == {("agent", "none", "clean", "syn-01", "truth")}
    assert out.read_text(encoding="utf-8") == json.dumps(rec) + "\n"


def test_done_keys_skips_blank_lines(tmp_path):
    rec = {"system": "agent", "strategy": "none", "scenario": "clean", "company_id": "syn-01",
           "approver": "truth"}
    (tmp_path / "other.jsonl").write_text("\n" + json.dumps(rec) + "\n\n", encoding="utf-8")
    assert done_keys(tmp_path / "runs.jsonl") == {("agent", "none", "clean", "syn-01", "truth")}


def test_real_limit_detects_upstream_quota_only():
    gateway = ("<OpenAPI_ServiceResponse><cmmMsgHeader>"
               "<errMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</errMsg>"
               "<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
    assert real_limit("fsc", httpx.Response(200, text=gateway))
    assert real_limit("nts", httpx.Response(429, text=""))
    assert real_limit("dart", httpx.Response(200, json={"status": "020", "message": "요청 제한 초과"}))
    assert real_limit("dart", httpx.Response(200, json={"status": "000"})) is None
    assert real_limit("fsc", httpx.Response(200, json={"response": {"header": {"resultCode": "00"}}})) is None


async def test_run_matrix_stops_without_recording_on_upstream_limit(make_stack, companies, tmp_path):
    out = tmp_path / "runs.jsonl"
    ledger = CallLedger(tmp_path / "calls.json", {"fsc": 100})
    async with make_stack() as stack:
        stack.proxy.upstream_limit, stack.proxy.limit_agency = "fsc: 공공데이터포털 한도 초과", "fsc"
        with pytest.raises(UpstreamLimit):
            await run_matrix(stack, [truth_of(by_id(companies, "syn-01"))], ["baseline"], [Strategy.NONE],
                             select(["clean"]), StubReportWriter(), InMemorySaver(), out, FAST,
                             ledger=ledger)
    assert out.read_text(encoding="utf-8") == ""
    # 버린 실행의 호출도 세고, 같은 날 다시 돌리면 바로 멈춘다
    again = CallLedger(tmp_path / "calls.json", {"fsc": 100})
    assert again.used["fsc"] >= 1 and again.exhausted() == ["fsc"]


async def test_rerun_into_another_file_skips_runs_recorded_elsewhere(make_stack, companies, tmp_path):
    truths = [truth_of(by_id(companies, "syn-01"))]
    async with make_stack() as stack:
        await run_matrix(stack, truths, ["baseline"], [Strategy.NONE], select(["clean"]),
                         StubReportWriter(), InMemorySaver(), tmp_path / "baseline.jsonl", FAST)
        inject = await run_matrix(stack, truths, ["baseline"], [Strategy.NONE, Strategy.RETRY],
                                  select(["clean"]), StubReportWriter(), InMemorySaver(),
                                  tmp_path / "inject.jsonl", FAST)
    assert [r["strategy"] for r in inject] == ["retry"]


def test_compute_counts_a_run_recorded_twice_once(companies, tmp_path):
    truth = truth_of(by_id(companies, "syn-01"))
    (tmp_path / "truth.jsonl").write_text(json.dumps(truth) + "\n", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    rec = {"system": "baseline", "strategy": "none", "scenario": "clean", "scenario_agency": None,
           "scenario_kind": None, "scenario_mode": None, "company_id": "syn-01", "status": "done",
           "interrupted": False, "report": None, "hitl": 0, "approver": "first", "received": {},
           "forwarded": {}, "input_tokens": 0, "output_tokens": 0, "model": None, "elapsed_s": 1.0}
    for name in ("a.jsonl", "b.jsonl"):
        (runs / name).write_text(json.dumps(rec) + "\n", encoding="utf-8")
    result = compute(runs, tmp_path / "truth.jsonl")
    assert result["n_records"] == 1 and result["n_duplicates_dropped"] == 1


def test_call_ledger_sums_across_instances(tmp_path):
    path = tmp_path / "calls.json"
    first = CallLedger(path, {"fsc": 3})
    first.add({"fsc": 2, "nts": 1})
    second = CallLedger(path, {"fsc": 3})
    assert second.used["fsc"] == 2 and second.exhausted() == []
    second.add({"fsc": 1})
    assert second.exhausted() == ["fsc"]
    assert parse_budget(["fsc=9000", "dart=19000"]) == {"fsc": 9000, "dart": 19000}


def test_call_ledger_starts_fresh_on_a_new_day(tmp_path, monkeypatch):
    import datetime as dt

    from vendor_check.eval import run as run_mod

    today = {"d": dt.date(2026, 1, 1)}
    monkeypatch.setattr(run_mod, "date", type("D", (), {"today": staticmethod(lambda: today["d"])}))
    ledger = CallLedger(tmp_path / "calls.json", {"fsc": 2})
    ledger.add({"fsc": 2})
    ledger.block("nts")
    assert ledger.exhausted() == ["fsc", "nts"]
    today["d"] = dt.date(2026, 1, 2)
    assert ledger.exhausted() == []
    ledger.add({"fsc": 1})
    data = json.loads((tmp_path / "calls.json").read_text(encoding="utf-8"))
    assert data == {"2026-01-01": {"fsc": 2, "blocked:nts": 1}, "2026-01-02": {"fsc": 1}}


async def test_run_matrix_stops_when_daily_budget_is_used(make_stack, companies, tmp_path):
    truths = [truth_of(by_id(companies, i)) for i in ("syn-01", "syn-02b")]
    ledger = CallLedger(tmp_path / "calls.json", {"fsc": 1})
    out = tmp_path / "runs.jsonl"
    async with make_stack() as stack:
        with pytest.raises(UpstreamLimit):
            await run_matrix(stack, truths, ["baseline"], [Strategy.NONE], select(["clean"]),
                             StubReportWriter(), InMemorySaver(), out, FAST, ledger=ledger)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    assert ledger.used["fsc"] >= 1
