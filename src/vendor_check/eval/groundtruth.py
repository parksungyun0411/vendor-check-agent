"""정답 생성: 평가 대상 기업 목록을 프록시 없이 기관 API 로 직접 조회해 jsonl 로 남긴다.

입력 CSV 열: id, corp_name, bzno  (bzno 는 동명 법인 중 대상을 고르는 데 쓴다)
조회가 하나라도 실패한 회사는 complete=false 로 남기고 실험에서 뺀다.

    python -m vendor_check.eval.groundtruth --companies data/companies.csv --out data/ground_truth.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
from pathlib import Path

import httpx

from vendor_check.clients.nts import normalize_bno
from vendor_check.config import Settings
from vendor_check.gateway import DirectGateway, ToolGateway
from vendor_check.pipeline import Steps, fsc_values
from vendor_check.recovery import Strategy, draft_report
from vendor_check.tools import AgencyTools


async def build_truth(gateway: ToolGateway, row: dict) -> dict:
    steps = Steps(gateway, Strategy.NONE)
    want = normalize_bno(row["bzno"])
    rec = {"id": row["id"], "corp_name": row["corp_name"], "complete": False, "fields": {}}
    found = await steps.call("search_corp", {"corp_name": row["corp_name"]})
    if not found["ok"]:
        rec["reason"] = f"search_corp failed: {found['error']['kind']}"
        return rec
    entity = next((c for c in found["data"] if normalize_bno(c.get("bzno") or "") == want), None)
    if entity is None:
        rec["reason"] = "no candidate with matching bzno"
        return rec
    sources = {
        "fsc": fsc_values(True, entity),
        "nts": await steps.nts(entity["bzno"]),
        "dart": await steps.dart(entity["corp_name"], match_crno=entity.get("crno")),
    }
    failed = [a for a, v in sources.items() if v is None]
    if failed:
        rec["reason"] = f"lookup failed: {failed}"
        return rec
    report = draft_report(sources, Strategy.NONE)
    rec["fields"] = {f: e["value"] for f, e in report.items()}
    rec["n_candidates"] = len(found["data"])
    rec["complete"] = True
    return rec


async def _main(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    if not settings.data_go_kr_key or not settings.dart_key:
        raise SystemExit("DATA_GO_KR_SERVICE_KEY 와 OPENDART_API_KEY 가 필요합니다 (.env.example 참고).")
    with open(args.companies, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient() as http:
        gateway = DirectGateway(AgencyTools(settings, http))
        with out.open("w", encoding="utf-8") as sink:
            for row in rows:
                rec = await build_truth(gateway, row)
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(row["id"], "ok" if rec["complete"] else rec.get("reason"))
                await asyncio.sleep(args.pause)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--companies", default="data/companies.csv")
    p.add_argument("--out", default="data/ground_truth.jsonl")
    p.add_argument("--pause", type=float, default=0.2, help="회사 사이 대기(초), 초당 한도 대비")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
