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
import os
import time
import uuid
from collections import Counter
from datetime import date
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
        hitl = out.get("hitl", 0)
        if out["status"] == "interrupted":
            interrupted = True
            proxy.reset_quota()
            out = await run_baseline(stack.gateway, query, strategy, policy, approve)  # 처음부터 다시
            hitl += out.get("hitl", 0)  # 처음부터 다시 돌면 확인도 다시 묻는다
        usage = []
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


# 개발계정 하루 한도(금융위 10,000, OpenDART 20,000)보다 조금 낮게 잡는다. 표본 추출·정답 생성은 따로 센다.
DEFAULT_BUDGET = {"fsc": 9000, "dart": 19000}


class CallLedger:
    """기관별 하루 업스트림 호출 수(이 머신의 날짜 기준). 하루에 여러 번 나눠 돌려도 함께 센다."""

    def __init__(self, path: Path, budget: dict[str, int]):
        self.path, self.budget = path, budget
        self.day, self.used = "", Counter()
        self._roll()

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day, self.used = today, Counter(self._load().get(today, {}))

    def add(self, forwarded: dict) -> None:
        self._roll()
        self.used.update(forwarded)
        data = self._load()
        data[self.day] = dict(self.used)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")  # 쓰다 죽어도 장부가 깨지지 않게 바꿔치기한다
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def block(self, agency: str) -> None:
        """실제 한도 초과를 본 기관. 그날은 같은 명령을 다시 돌려도 바로 멈춘다."""
        self.add({f"blocked:{agency}": 1})

    def exhausted(self) -> list[str]:
        self._roll()
        over = [a for a, n in self.budget.items() if self.used[a] >= n]
        return over + [k.split(":", 1)[1] for k in self.used if k.startswith("blocked:")]


class UpstreamLimit(RuntimeError):
    """실제 기관 API 의 한도 초과. 그 실행은 기록하지 않고 멈춘다. 다음 날 같은 명령으로 이어 간다."""


def run_key(r: dict) -> tuple:
    """실행 하나를 가리키는 키. 같은 키는 같은 조건의 반복이라 한 번만 센다."""
    return (r["system"], r["strategy"], r["scenario"], r["company_id"], r["approver"])


def done_keys(out_path: Path) -> set[tuple]:
    """out_path 와 같은 폴더의 기록에 이미 있는 실행. 끊긴 뒤 같은 명령으로 이어 돌거나, 무주입 실행
    (make baseline agent)을 이미 한 뒤 전체 행렬(make inject)을 돌 때 겹치는 조합을 건너뛴다.

    out_path 의 마지막 줄이 쓰다 만 줄이면 지운다(flush 는 줄 단위라 중간 줄은 깨지지 않는다)."""
    keys: set[tuple] = set()
    if not out_path.parent.exists():
        return keys
    for f in sorted(out_path.parent.glob("*.jsonl")):
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                if f != out_path or i != len(lines) - 1:
                    raise SystemExit(f"{f}:{i + 1} 줄이 깨졌습니다. 확인 후 다시 실행하세요.") from e
                f.write_text("".join(x + "\n" for x in lines[:-1]), encoding="utf-8")
                print(f"[resume] {f} 마지막 줄이 쓰다 만 줄이라 지웠습니다")
                break
            keys.add(run_key(r))
    return keys


async def run_matrix(stack: Stack, truths: list[dict], systems: list[str],
                     strategies: list[Strategy], scenarios: list[Scenario], writer: ReportWriter,
                     checkpointer, out_path: Path | None = None,
                     policy: RetryPolicy | None = None, baseline_approver: str = "first",
                     ledger: CallLedger | None = None) -> list[dict]:
    run_id = uuid.uuid4().hex[:8]
    records = []
    done = done_keys(out_path) if out_path else set()
    skipped = 0
    sink = out_path.open("a", encoding="utf-8") if out_path else None
    try:
        for scenario in scenarios:
            for strategy in strategies:
                for system in systems:
                    approver = "truth" if system == "agent" else baseline_approver
                    for truth in truths:
                        if (system, str(strategy), scenario.name, truth["id"], approver) in done:
                            skipped += 1
                            continue
                        if ledger and ledger.exhausted():
                            raise UpstreamLimit(f"오늘 호출 예산 소진 {dict(ledger.used)} "
                                                f"(기록 {len(records)}건)")
                        rec = await run_one(stack, system, strategy, scenario, truth, writer,
                                            checkpointer, run_id, policy, baseline_approver)
                        if stack.proxy.upstream_limit:
                            if ledger:  # 버리는 실행도 호출은 했다
                                ledger.add(rec["forwarded"])
                                ledger.block(stack.proxy.limit_agency or "unknown")
                            raise UpstreamLimit(f"{stack.proxy.upstream_limit} (기록 {len(records)}건, "
                                                f"건너뜀 {skipped}건, 마지막 실행은 버림)")
                        if ledger:
                            ledger.add(rec["forwarded"])
                        records.append(rec)
                        if sink:
                            sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            sink.flush()
    finally:
        if sink:
            sink.close()
    if skipped:
        print(f"[resume] 이미 기록된 {skipped}건을 건너뛰었습니다")
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
        try:
            records = await run_matrix(
                stack, truths, args.systems, [Strategy(s) for s in args.strategies],
                select(args.scenarios), build_writer(settings), saver, out,
                baseline_approver=args.baseline_approver,
                ledger=CallLedger(settings.state_dir / "calls.json", parse_budget(args.budget)),
            )
        except UpstreamLimit as e:
            raise SystemExit(f"멈춤: {e}. 다음 날 같은 명령으로 이어 갑니다(기록된 조합은 건너뜀).") from e
    print(f"{len(records)} records → {out}")


def parse_budget(items: list[str]) -> dict[str, int]:
    return {k: int(v) for k, v in (i.split("=", 1) for i in items)}


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
    p.add_argument("--budget", nargs="*", default=[f"{k}={v}" for k, v in DEFAULT_BUDGET.items()],
                   help="기관별 하루 업스트림 호출 예산(예: fsc=9000 dart=19000). 닿으면 멈춘다")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
