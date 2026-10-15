"""지표 계산. 입력은 실행 기록(jsonl 한 줄 = 회사 한 곳 점검 한 번)과 정답.

- 항목 일치율: (기록, 항목) 쌍 중 status=ok 이고 값이 정답과 같은 비율
- 점검 성공: 한 기록의 모든 항목이 정답과 일치
- 실패 유형 × 전략 성공률: 시나리오 실패 유형·모드별 점검 성공 비율
- 재개 성공률: 한도 초과로 멈춘 기록 중, 재개 후 점검 성공한 비율
- 조용한 오답 비율: status=ok 로 보고한 항목 중 값이 정답과 다른 비율
- 건당 토큰·비용·p95 지연

시스템은 baseline(첫 후보), baseline+confirm(에이전트와 같은 동명 법인 확인), agent 로 나눠 센다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from vendor_check.recovery import FIELDS


def norm(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.replace("-", "").replace(" ", "").isdigit():
        return s.replace("-", "").replace(" ", "")
    return " ".join(s.split())


def field_outcomes(record: dict, truth: dict) -> list[tuple[str, bool, bool]]:
    """(항목, ok로 보고했는가, 정답과 일치하는가) 목록."""
    report = record.get("report") or {}
    out = []
    for f in FIELDS:
        entry = report.get(f) or {"value": None, "status": "unknown"}
        reported_ok = entry.get("status") == "ok"
        correct = reported_ok and norm(entry.get("value")) == norm(truth.get(f))
        out.append((f, reported_ok, correct))
    return out


def system_label(record: dict) -> str:
    """대조군에 에이전트와 같은 동명 법인 확인을 준 기록은 따로 집계한다."""
    if record["system"] == "baseline" and record.get("approver") == "truth":
        return "baseline+confirm"
    return record["system"]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, math.ceil(q / 100 * len(ordered)) - 1)
    return ordered[k]


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def summarize(records: Iterable[dict], truth: dict[str, dict],
              price_in: float | None = None, price_out: float | None = None) -> dict:
    groups: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    latency: dict[tuple, list[float]] = defaultdict(list)
    tokens: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    by_system: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    for r in records:
        if r["company_id"] not in truth:
            continue
        gt = truth[r["company_id"]]["fields"]
        system = system_label(r)
        key = (system, r["strategy"], r["scenario_kind"] or "clean", r["scenario_mode"] or "-")
        g = groups[key]
        outcomes = field_outcomes(r, gt)
        g["checks"] += 1
        g["fields"] += len(outcomes)
        g["agree"] += sum(c for _, _, c in outcomes)
        g["reported_ok"] += sum(ok for _, ok, _ in outcomes)
        g["silent_wrong"] += sum(ok and not c for _, ok, c in outcomes)
        full = r["status"] == "done" and all(c for _, _, c in outcomes)
        g["success"] += full
        if r.get("interrupted"):
            g["interrupted"] += 1
            g["resumed_ok"] += full
        latency[key].append(r["elapsed_s"])
        tokens[key].append((r.get("input_tokens", 0), r.get("output_tokens", 0)))
        s = by_system[system]
        s["fields"] += len(outcomes)
        s["agree"] += sum(c for _, _, c in outcomes)
        s["reported_ok"] += sum(ok for _, ok, _ in outcomes)
        s["silent_wrong"] += sum(ok and not c for _, ok, c in outcomes)

    rows = []
    for key, g in sorted(groups.items()):
        system, strategy, kind, mode = key
        tin = [t[0] for t in tokens[key]]
        tout = [t[1] for t in tokens[key]]
        cost = None
        if price_in is not None and price_out is not None and g["checks"]:
            cost = (sum(tin) * price_in + sum(tout) * price_out) / 1e6 / g["checks"]
        rows.append({
            "system": system, "strategy": strategy, "fault_kind": kind, "fault_mode": mode,
            "checks": g["checks"],
            "item_agreement": _rate(g["agree"], g["fields"]),
            "check_success": _rate(g["success"], g["checks"]),
            "silent_wrong_rate": _rate(g["silent_wrong"], g["reported_ok"]),
            "resume_success": _rate(g["resumed_ok"], g["interrupted"]),
            "interrupted": g["interrupted"],
            "tokens_in_per_check": _rate(sum(tin), g["checks"]),
            "tokens_out_per_check": _rate(sum(tout), g["checks"]),
            "cost_per_check": cost,
            "p95_latency_s": percentile(latency[key], 95),
        })
    overall = {
        system: {"item_agreement": _rate(s["agree"], s["fields"]),
                 "silent_wrong_rate": _rate(s["silent_wrong"], s["reported_ok"])}
        for system, s in sorted(by_system.items())
    }
    return {"groups": rows, "overall": overall}
