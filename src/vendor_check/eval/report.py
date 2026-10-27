"""지표 집계(metrics)와 결과표 생성(render).

    python -m vendor_check.eval.report metrics   # reports/runs/*.jsonl → reports/metrics.json
    python -m vendor_check.eval.report render    # reports/metrics.json → reports/RESULTS.md

실측 기록이 없으면 표를 비워 두고 "측정 전"이라고만 적는다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vendor_check.config import Settings
from vendor_check.eval.metrics import summarize
from vendor_check.eval.run import load_truth, run_key

PENDING = "측정 전 — `make eval` 실행 후 자동 생성"


def _fmt(v: object, pct: bool = True) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v * 100:.1f}%" if pct else f"{v:.3f}"
    return str(v)


def compute(runs_dir: Path, truth_path: Path) -> dict | None:
    files = sorted(runs_dir.glob("*.jsonl")) if runs_dir.exists() else []
    if not files or not truth_path.exists():
        return None
    records, seen, duplicates = [], set(), 0
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            r = json.loads(line)
            if run_key(r) in seen:  # 같은 조건을 두 파일에 기록한 경우 처음 것만 센다
                duplicates += 1
                continue
            seen.add(run_key(r))
            records.append(r)
    settings = Settings.from_env()
    result = summarize(records, load_truth(truth_path),
                       settings.price_in_per_mtok, settings.price_out_per_mtok)
    result["sources"] = [str(f) for f in files]
    result["n_records"] = len(records)
    result["n_duplicates_dropped"] = duplicates
    return result


def render(metrics: dict | None) -> str:
    lines = ["# 결과", ""]
    if metrics is None:
        lines += [PENDING, "",
                  "| 시스템 | 복구 전략 | 실패 유형 | 모드 | 점검 성공률 | 항목 일치율 "
                  "| 조용한 오답 비율 | 재개 성공률 | 건당 토큰(입/출) | 건당 비용 | p95 지연(s) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|",
                  "|  |  |  |  |  |  |  |  |  |  |  |"]
        return "\n".join(lines) + "\n"
    lines += [f"기록 {metrics['n_records']}건, 원본: {', '.join(metrics['sources'])}", "",
              "## 시스템별 전체", "", "| 시스템 | 항목 일치율 | 조용한 오답 비율 |", "|---|---|---|"]
    for system, o in metrics["overall"].items():
        lines.append(f"| {system} | {_fmt(o['item_agreement'])} | {_fmt(o['silent_wrong_rate'])} |")
    lines += ["", "## 실패 유형 × 복구 전략", "",
              "| 시스템 | 복구 전략 | 실패 유형 | 모드 | 건수 | 점검 성공률 | 항목 일치율 "
              "| 조용한 오답 비율 | 재개 성공률 | 건당 토큰(입/출) | 건당 비용 | p95 지연(s) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for g in metrics["groups"]:
        tok = ""
        if g["tokens_in_per_check"] is not None:
            tok = f"{g['tokens_in_per_check']:.0f} / {g['tokens_out_per_check']:.0f}"
        cost = f"${g['cost_per_check']:.5f}" if g["cost_per_check"] is not None else ""
        lines.append(
            f"| {g['system']} | {g['strategy']} | {g['fault_kind']} | {g['fault_mode']} "
            f"| {g['checks']} | {_fmt(g['check_success'])} | {_fmt(g['item_agreement'])} "
            f"| {_fmt(g['silent_wrong_rate'])} | {_fmt(g['resume_success'])} | {tok} | {cost} "
            f"| {_fmt(g['p95_latency_s'], pct=False)} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["metrics", "render"])
    p.add_argument("--runs", default="reports/runs")
    p.add_argument("--truth", default="data/ground_truth.jsonl")
    p.add_argument("--metrics", default="reports/metrics.json")
    p.add_argument("--out", default="reports/RESULTS.md")
    args = p.parse_args()
    metrics_path = Path(args.metrics)
    if args.command == "metrics":
        result = compute(Path(args.runs), Path(args.truth))
        if result is None:
            print("실측 기록 또는 정답 파일이 없습니다. metrics.json 을 만들지 않습니다.")
            return
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ {metrics_path}")
    else:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() \
            else None
        Path(args.out).write_text(render(metrics), encoding="utf-8")
        print(f"→ {args.out}")


if __name__ == "__main__":
    main()
