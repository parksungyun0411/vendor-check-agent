"""평가 대상 회사 표본: OpenDART 고유번호 목록에서 층별로 뽑아 data/companies.csv 를 만든다.

층(기본 100곳): 상장 30 · 비상장 30 · 휴·폐업 20 · 동명 20.
- 상장·비상장: DART 기업개황 corp_cls 가 Y·K·N 이면 상장, E 면 비상장. 국세청 상태가 계속사업자인 것만.
- 휴·폐업: 국세청 상태가 휴업자·폐업자인 것. 상장·비상장 후보를 번갈아 조회하다 나온 것을 쓴다.
- 동명: DART 목록에 같은 정식 회사명이 둘 이상인 이름에서 하나를 고르고, 금융위 검색 결과에도
  그 이름의 다른 법인(법인등록번호)이 둘 이상 나와야 한다.
모든 층 공통: 사업자번호가 있고, 금융위 상호 검색(상위 20건)에 그 사업자번호를 가진 법인이 있어야 한다.
정답 생성(groundtruth)이 같은 검색으로 대상을 찾기 때문이다. 탈락 사유는 --stats 파일에 센다.

고유번호 목록은 날마다 바뀌므로, 같은 시드라도 목록을 받은 날이 다르면 표본이 달라진다.

    uv run python -m vendor_check.eval.sample --seed 42 --out data/companies.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import httpx

from vendor_check.clients.nts import normalize_bno
from vendor_check.config import Settings
from vendor_check.tools import AgencyTools

QUOTA = {"listed": 30, "unlisted": 30, "closed": 20, "dup": 20}
LISTED_MARKETS = {"KOSPI", "KOSDAQ", "KONEX"}
CLOSED_STT = {"02", "03"}  # 국세청 b_stt_cd: 02 휴업자, 03 폐업자
ID_PREFIX = {"listed": "lst", "unlisted": "unl", "closed": "cls", "dup": "dup"}


def build_pools(corps: list[dict], rng: random.Random) -> dict[str, list[dict]]:
    """DART 고유번호 목록을 상장·비상장·동명 후보로 나누고 섞는다. 동명은 이름마다 한 법인만 넣는다."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for c in sorted(corps, key=lambda c: c["corp_code"]):
        by_name[c["corp_name"]].append(c)
    pools: dict[str, list[dict]] = {"listed": [], "unlisted": [], "dup": []}
    for name in sorted(by_name):
        group = by_name[name]
        if len(group) >= 2:
            pools["dup"].append(rng.choice(group))
        else:
            pools["listed" if group[0]["stock_code"] else "unlisted"].append(group[0])
    for p in pools.values():
        rng.shuffle(p)
    return pools


def prospective(pool: str, company: dict | None, status: dict | None) -> str:
    """금융위 검색 전, DART 기업개황과 국세청 상태로 정한 층. 탈락이면 'drop:사유'."""
    if company is None:
        return "drop:dart_not_found"
    if not company.get("bzno"):
        return "drop:no_bzno"
    if status is None:
        return "drop:nts_failed"
    if not status.get("registered"):
        return "drop:nts_unregistered"
    if pool == "dup":
        return "dup"
    if status.get("b_stt_cd") in CLOSED_STT:
        return "closed"
    if status.get("b_stt_cd") != "01":
        return "drop:nts_unknown_status"
    if pool == "listed":
        return "listed" if company.get("market") in LISTED_MARKETS else "drop:not_listed"
    return "unlisted" if company.get("market") == "NONE" else "drop:market_mismatch"


def fsc_check(stratum: str, company: dict, rows: list[dict] | None) -> str | None:
    """금융위 검색 결과로 확인. 통과면 None, 탈락이면 'drop:사유'."""
    if rows is None:
        return "drop:fsc_failed"
    want = normalize_bno(company["bzno"])
    target = next((r for r in rows if normalize_bno(r.get("bzno") or "") == want), None)
    if target is None:
        return "drop:fsc_no_match"
    if stratum == "dup":
        same = {r.get("crno") for r in rows if r.get("corp_name") == target.get("corp_name")}
        if len(same) < 2:
            return "drop:fsc_not_homonym"
    return None


async def draw(tools: AgencyTools, corps: list[dict], rng: random.Random, quota: dict[str, int],
               max_tries: int, pause: float) -> tuple[list[dict], dict]:
    pools = build_pools(corps, rng)
    picked: dict[str, list[dict]] = {k: [] for k in quota}
    tried: Counter = Counter()
    outcomes: Counter = Counter()

    def full(stratum: str) -> bool:
        return len(picked[stratum]) >= quota[stratum]

    # 휴·폐업은 상장·비상장 후보 양쪽에서 나온다. 한 풀을 먼저 다 돌면 휴·폐업이 그 풀에서만 채워지므로
    # (첫 실행에서 20곳 모두 종목코드가 있던 회사였다) 두 풀을 번갈아 돈다.
    serves = {"listed": ("listed", "closed"), "unlisted": ("unlisted", "closed"), "dup": ("dup",)}
    streams = [list(_alternate(pools["listed"], pools["unlisted"])), [("dup", c) for c in pools["dup"]]]
    source: Counter = Counter()
    for stream in streams:
        need = {s for pool in {p for p, _ in stream} for s in serves[pool]}
        for pool, cand in stream:
            if all(full(s) for s in need):
                break
            if all(full(s) for s in serves[pool]) or tried[pool] >= max_tries:
                continue
            tried[pool] += 1
            await asyncio.sleep(pause)
            r = await tools.dart_company(cand["corp_code"])
            if not r["ok"]:
                outcomes[f"drop:dart_{r['error']['kind']}"] += 1
                if r["error"]["kind"] == "quota":
                    break
                continue
            company = r["data"]
            status = None
            if company and company.get("bzno"):
                s = await tools.business_status(company["bzno"])
                status = s["data"] if s["ok"] else None
            stratum = prospective(pool, company, status)
            if stratum.startswith("drop:"):
                outcomes[stratum] += 1
                continue
            if full(stratum):
                outcomes[f"skip:{stratum}_full"] += 1
                continue
            f = await tools.search_corp(cand["corp_name"])
            reason = fsc_check(stratum, company, f["data"] if f["ok"] else None)
            if reason:
                outcomes[reason] += 1
                continue
            picked[stratum].append({"corp_name": cand["corp_name"], "bzno": company["bzno"]})
            outcomes[f"ok:{stratum}"] += 1
            source[f"{stratum}<-{pool}"] += 1

    rows = [{"id": f"{ID_PREFIX[s]}-{i:02d}", **c}
            for s in quota for i, c in enumerate(picked[s], start=1)]
    stats = {
        "quota": quota,
        "picked": {s: len(v) for s, v in picked.items()},
        "pool_sizes": {p: len(v) for p, v in pools.items()},
        "tried": dict(tried),
        "picked_from_pool": dict(sorted(source.items())),
        "outcomes": dict(sorted(outcomes.items())),
    }
    return rows, stats


def _alternate(listed: list[dict], unlisted: list[dict]):
    """상장·비상장 후보를 번갈아 내놓는다. 짧은 쪽이 끝나면 긴 쪽만 이어진다."""
    for i in range(max(len(listed), len(unlisted))):
        if i < len(listed):
            yield "listed", listed[i]
        if i < len(unlisted):
            yield "unlisted", unlisted[i]


async def _main(args: argparse.Namespace) -> None:
    runs = [f for f in Path(args.runs).glob("*.jsonl") if f.stat().st_size]
    if runs:
        # 기록은 회사 id(lst-01 등)로 정답과 짝짓는다. 표본을 다시 뽑으면 같은 id가 다른 회사를 가리킨다.
        raise SystemExit(f"{args.runs} 에 실행 기록 {len(runs)}개가 있어 표본을 다시 뽑지 않습니다. "
                         "옛 기록을 다른 곳으로 옮긴 뒤 다시 실행하세요.")
    settings = Settings.from_env()
    if not settings.data_go_kr_key or not settings.dart_key:
        raise SystemExit("DATA_GO_KR_SERVICE_KEY 와 OPENDART_API_KEY 가 필요합니다 (.env.example 참고).")
    async with httpx.AsyncClient() as http:
        tools = AgencyTools(settings, http)
        corps = await tools._corp_index()
        rows, stats = await draw(tools, corps, random.Random(args.seed), QUOTA, args.max_tries,
                                 args.pause)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "corp_name", "bzno"])
        w.writeheader()
        w.writerows(rows)
    stats = {"seed": args.seed, "drawn_on": date.today().isoformat(), "corpcode_rows": len(corps)} | stats
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(stats["picked"], ensure_ascii=False), "→", out)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="data/companies.csv")
    p.add_argument("--stats", default="reports/sampling.json", help="층별 개수·탈락 사유 (회사 이름 없음)")
    p.add_argument("--runs", default="reports/runs", help="실행 기록 폴더. 기록이 있으면 다시 뽑지 않는다")
    p.add_argument("--max-tries", type=int, default=2000, help="풀마다 조회할 후보 상한")
    p.add_argument("--pause", type=float, default=0.2, help="후보 사이 대기(초), 초당 한도 대비")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
