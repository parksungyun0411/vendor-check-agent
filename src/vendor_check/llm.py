"""보고서 작성 LLM. 공급자 인터페이스 뒤에 두고, 테스트는 결정적 스텁을 쓴다.

LLM 은 병합된 점검 항목 초안과 실패 기록을 받아 최종 보고서(JSON)와 요약 문장을 쓴다.
값을 바꾸거나 실패한 항목을 정상으로 채우면 그대로 "조용한 오답" 지표에 잡힌다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from vendor_check.config import Settings
from vendor_check.recovery import FIELDS


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "stub"


class ReportWriter(Protocol):
    async def write(self, query: dict, draft: dict, failures: list[dict]) -> tuple[dict, str, Usage]:
        ...


class StubReportWriter:
    """초안을 그대로 돌려주는 결정적 스텁. 토큰은 0으로 기록한다."""

    def __init__(self) -> None:
        self.calls = 0

    async def write(self, query: dict, draft: dict, failures: list[dict]) -> tuple[dict, str, Usage]:
        self.calls += 1
        unknown = [f for f, e in draft.items() if e["status"] != "ok"]
        summary = f"{query['corp_name']}: 확인 {len(draft) - len(unknown)}개, 미확인 {unknown}"
        return draft, summary, Usage()


SYSTEM_PROMPT = """너는 거래처 점검 보고서를 정리한다.
입력의 draft 는 공공 API 조회 결과를 항목별로 합친 것이고 failures 는 실패한 툴 호출 기록이다.
- draft 에 있는 값만 쓴다. 값을 추측하거나 다른 항목에서 옮겨 오지 않는다.
- status 가 unknown 인 항목은 value 를 null 로 두고 unknown 을 유지한다.
- summary 에는 확인한 것과 확인하지 못한 것을 한국어 두세 문장으로 적는다."""

_ENTRY = {
    "type": "object",
    "properties": {
        "value": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": ["ok", "unknown"]},
        "source": {"type": ["string", "null"]},
    },
    "required": ["value", "status", "source"],
    "additionalProperties": False,
}
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "report": {
            "type": "object",
            "properties": {f: _ENTRY for f in FIELDS},
            "required": list(FIELDS),
            "additionalProperties": False,
        },
        "summary": {"type": "string"},
    },
    "required": ["report", "summary"],
    "additionalProperties": False,
}


class AnthropicReportWriter:
    """Claude Messages API. 키가 없으면 만들 수 없다. `pip install -e '.[llm]'` 필요."""

    def __init__(self, model: str):
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = model

    async def write(self, query: dict, draft: dict, failures: list[dict]) -> tuple[dict, str, Usage]:
        payload = json.dumps({"query": query, "draft": draft, "failures": failures},
                             ensure_ascii=False)
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": payload}],
        )
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens, self.model)
        if response.stop_reason == "refusal":
            return draft, "LLM 이 요청을 거절해 초안을 그대로 썼습니다.", usage
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return data["report"], data["summary"], usage


def build_writer(settings: Settings) -> ReportWriter:
    if settings.llm_provider == "anthropic":
        return AnthropicReportWriter(settings.llm_model)
    return StubReportWriter()
