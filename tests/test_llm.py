"""Anthropic 보고서 작성기의 요청 형태와 응답 처리. 네트워크 없이 클라이언트를 바꿔 끼운다."""

import json
from types import SimpleNamespace

import pytest

from vendor_check.llm import REPORT_SCHEMA, AnthropicReportWriter
from vendor_check.recovery import FIELDS


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def make_writer(response) -> tuple[AnthropicReportWriter, FakeMessages]:
    pytest.importorskip("anthropic")
    writer = AnthropicReportWriter.__new__(AnthropicReportWriter)
    messages = FakeMessages(response)
    writer.client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    writer.model = "claude-opus-5-5"
    return writer, messages


DRAFT = {f: {"value": "v", "status": "ok", "source": "fsc"} for f in FIELDS}


async def test_parses_structured_output_and_usage():
    body = {"report": DRAFT, "summary": "모두 확인했습니다."}
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(body, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=321, output_tokens=45),
    )
    writer, messages = make_writer(response)
    report, summary, usage = await writer.write({"corp_name": "x"}, DRAFT, [])
    assert report == DRAFT and summary == "모두 확인했습니다."
    assert (usage.input_tokens, usage.output_tokens) == (321, 45)
    assert messages.kwargs["output_config"]["format"]["schema"] is REPORT_SCHEMA
    assert messages.kwargs["fallbacks"] == "default"


async def test_refusal_falls_back_to_draft():
    response = SimpleNamespace(stop_reason="refusal", content=[],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=0))
    writer, _ = make_writer(response)
    report, _, usage = await writer.write({"corp_name": "x"}, DRAFT, [])
    assert report == DRAFT and usage.input_tokens == 10
