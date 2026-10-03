"""복구 전략 4종과, 대조군·에이전트가 같이 쓰는 점검 항목 병합.

전략은 서로 섞지 않는다. 실패 유형 × 전략 표에서 효과를 전략 하나에 귀속시키기 위해서다.
  none        한 번 부르고, 실패하면 그 항목은 unknown
  retry       재시도 가능한 실패(timeout·5xx·게이트웨이 01/05·malformed·partial·초당 한도)만
              지수 백오프로 최대 max_attempts 번
  fallback    재시도 없이, 실패한 기관의 항목을 다른 기관 값으로 채운다
  checkpoint  일일 한도 초과면 멈췄다가 다음 날 이어서 한다. 그 밖의 실패는 none 과 같다
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum

from vendor_check.gateway import ToolGateway

FIELDS = ("corp_name", "ceo_name", "crno", "bzno", "market", "b_stt_cd", "tax_type_cd")

# 정상 경로의 출처와, fallback 전략에서만 쓰는 대체 출처
PRIMARY = {"corp_name": "fsc", "ceo_name": "fsc", "crno": "fsc", "bzno": "fsc",
           "market": "dart", "b_stt_cd": "nts", "tax_type_cd": "nts"}
SECONDARY = {"corp_name": "dart", "ceo_name": "dart", "crno": "dart", "bzno": "dart",
             "market": "fsc"}


class Strategy(StrEnum):
    NONE = "none"
    RETRY = "retry"
    FALLBACK = "fallback"
    CHECKPOINT = "checkpoint"


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5


async def call_tool(gateway: ToolGateway, name: str, args: dict, strategy: Strategy,
                    policy: RetryPolicy | None = None) -> tuple[dict, dict]:
    """툴을 전략에 맞게 부른다. (결과, 호출 기록)을 돌려준다."""
    policy = policy or RetryPolicy()
    attempts = 0
    started = time.perf_counter()
    while True:
        attempts += 1
        result = await gateway.call(name, args)
        if result["ok"] or strategy != Strategy.RETRY:
            break
        if not result["error"].get("retryable") or attempts >= policy.max_attempts:
            break
        await asyncio.sleep(policy.base_delay_s * 2 ** (attempts - 1))
    log = {"tool": name, "ok": result["ok"], "attempts": attempts,
           "kind": None if result["ok"] else result["error"]["kind"],
           "elapsed_s": round(time.perf_counter() - started, 4)}
    return result, log


def is_quota(result: dict) -> bool:
    return not result["ok"] and result["error"]["kind"] == "quota"


def nts_values(record: dict | None) -> dict:
    if record is None:
        return {}
    stt = record.get("b_stt_cd") if record.get("registered") else "unregistered"
    return {"b_stt_cd": stt, "tax_type_cd": record.get("tax_type_cd")}


def draft_report(sources: dict[str, dict | None], strategy: Strategy) -> dict:
    """기관별 정규화 값(sources[agency] = 필드 dict 또는 실패 시 None)을 점검 항목으로 합친다."""
    report = {}
    for f in FIELDS:
        entry = {"value": None, "status": "unknown", "source": None}
        order = [PRIMARY[f]]
        if strategy == Strategy.FALLBACK and f in SECONDARY:
            order.append(SECONDARY[f])
        for agency in order:
            values = sources.get(agency)
            if values is not None and f in values:
                entry = {"value": values[f], "status": "ok", "source": agency}
                break
        report[f] = entry
    return report
