"""실험 러너: (시스템 × 복구 전략 × 실패 시나리오 × 회사) 를 돌려 jsonl 로 남긴다.

실측은 실제 기관 API 앞에 프록시를 끼워 돈다(키 필요). 한도 초과 시나리오에서는 멈춘 뒤
proxy.reset_quota() 로 "다음 날"을 만들고, 에이전트는 체크포인트에서 재개, 대조군은 처음부터
다시 돈다.

동명 법인 확인은 에이전트만 받는다(정답 승인자). --baseline-approver truth 를 주면 대조군도 같은
승인자로 법인을 고르고, 기록은 baseline+confirm 으로 따로 집계된다. 두 대조군을 함께 보면 확인
단계의 효과와 그래프 구조의 효과를 나눠 볼 수 있다.

    python -m vendor_check.eval.run --systems baseline agent --out reports/runs/run.jsonl
    python -m vendor_check.eval.run --systems baseline --baseline-approver truth \
        --out reports/runs/baseline-confirm.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid
from collections import Counter
from pathlib import Path

from vendor_check.config import DART_DEFAULT_BASE, FSC_DEFAULT_BASE, NTS_DEFAULT_BASE, Settings
from vendor_check.eval.scenarios import Scenario, select
from vendor_check.graph import build_graph, drive
from vendor_check.harness import Stack, open_stack
from vendor_check.llm import ReportWriter, build_writer
from vendor_check.pipeline import run_baseline
from vendor_check.recovery import RetryPolicy, Strategy

LIVE_UPSTREAMS = {"nts": NTS_DEFAULT_BASE, "fsc": FSC_DEFAULT_BASE, "dart": DART_DEFAULT_BASE}


def load_truth(path: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {r["id"]: r for r in rows if r.get("complete")}


def approver_for(truth: dict):
    """동명 법인 확인을 사람 대신 정답의 사업자번호로 답한다(평가용).

    에이전트는 항상, 대조군은 --baseline-approver truth 일 때 이 승인자를 쓴다.
    """

    def approve(query: dict, candidates: list[dict]) -> str:
        for key in ("bzno", "crno"):
            want = truth["fields"].get(key)
            for c in candidates:
                if want and c.get(key) == want:
                    return c["crno"]
        return candidates[0]["crno"]

    return approve


def _delta(after: Counter, before: dict) -> dict:
    return {k: after[k] - before.get(k, 0) for k in after if after[k] - before.get(k, 0)}


async def run_one(stack: Stack, system: str, strategy: Strategy, scenario: Scenario, truth: dict,
                  writer: ReportWriter, checkpointer, run_id: str,
                  policy: RetryPolicy | None = None, baseline_approver: str = "first") -> dict:
    proxy = stack.proxy
    proxy.set_rules(scenario.rules(stack.settings.timeout_s))
    received0, forwarded0 = dict(proxy.received), dict(proxy.forwarded)
    query = {"corp_name": truth["corp_name"]}
    started = time.perf_counter()
    interrupted = False

    if system == "baseline":
        approver = baseline_approver
        approve = approver_for(truth) if approver == "truth" else None
        out = await run_baseline(stack.gateway, query, strategy, policy, approve)
        if out["status"] == "interrupted":
            interrupted = True
            proxy.reset_quota()
            out = await run_baseline(stack.gateway, query, strategy, policy, approve)  # 처음부터 다시
        usage = []
        hitl = out.get("hitl", 0)
    else:
        approver = "truth"
        graph = build_graph(stack.gateway, writer, strategy, policy, checkpointer)
        thread_id = f"{run_id}:{scenario.name}:{strategy}:{truth['id']}"
        approve = approver_for(truth)
        out = await drive(graph, thread_id, approve, query=query)
        if out["status"] == "interrupted":
            interrupted = True
            proxy.reset_quota()
            out = await drive(graph, thread_id, approve, resume_quota=True)
        usage = out.get("usage", [])
        hitl = out.get("hitl", 0)

    return {
        "run_id": run_id, "system": system, "strategy": str(strategy),
        "scenario": scenario.name, "scenario_agency": scenario.agency,
        "scenario_kind": scenario.kind, "scenario_mode": scenario.mode,
        "company_id": truth["id"], "status": out["status"], "interrupted": interrupted,
        "report": out.get("report"), "hitl": hitl, "approver": approver,
        "received": _delta(proxy.received, received0),
        "forwarded": _delta(proxy.forwarded, forwarded0),
        "input_tokens": sum(u["input_tokens"] for u in usage),
        "output_tokens": sum(u["output_tokens"] for u in usage),
        "model": usage[0]["model"] if usage else None,
        "elapsed_s": round(time.perf_counter() - started, 4),
    }


async def run_matrix(stack: Stack, truths: list[dict], systems: list[str],
                     strategies: list[Strategy], scenarios: list[Scenario], writer: ReportWriter,
                     checkpointer, out_path: Path | None = None,
                     policy: RetryPolicy | None = None, baseline_approver: str = "first") -> list[dict]:
    run_id = uuid.uuid4().hex[:8]
    records = []
    sink = out_path.open("a", encoding="utf-8") if out_path else None
    try:
        for scenario in scenarios:
            for strategy in strategies:
                for system in systems:
                    for truth in truths:
                        rec = await run_one(stack, system, strategy, scenario, truth, writer,
                                            checkpointer, run_id, policy, baseline_approver)
                        records.append(rec)
                        if sink:
                            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            sink.flush()
    finally:
        if sink:
            sink.close()
    return records


async def _main(args: argparse.Namespace) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings = Settings.from_env()
    if not settings.data_go_kr_key or not settings.dart_key:
        raise SystemExit("DATA_GO_KR_SERVICE_KEY 와 OPENDART_API_KEY 가 필요합니다 (.env.example 참고).")
    truths = list(load_truth(Path(args.truth)).values())
    if args.limit:
        truths = truths[: args.limit]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    async with (
        open_stack(settings, LIVE_UPSTREAMS) as stack,
        AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver,
    ):
        records = await run_matrix(
            stack, truths, args.systems, [Strategy(s) for s in args.strategies],
            select(args.scenarios), build_writer(settings), saver, out,
            baseline_approver=args.baseline_approver,
        )
    print(f"{len(records)} records → {out}")


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--truth", default="data/ground_truth.jsonl")
    p.add_argument("--systems", nargs="+", default=["baseline", "agent"],
                   choices=["baseline", "agent"])
    p.add_argument("--strategies", nargs="+", default=[s.value for s in Strategy])
    p.add_argument("--scenarios", nargs="*", help="생략하면 전체 시나리오")
    p.add_argument("--baseline-approver", choices=["first", "truth"], default="first",
                   help="대조군의 동명 법인 선택. first=첫 후보(기본), truth=에이전트와 같은 정답 승인자")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="reports/runs/run.jsonl")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
