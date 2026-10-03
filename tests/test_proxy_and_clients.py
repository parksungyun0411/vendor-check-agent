"""프록시가 주입한 실패를 클라이언트가 어떻게 분류하는지 (strict) 와,
본문 검사를 끈 lenient 가 어떤 실패를 정상으로 통과시키는지."""

import pytest

from vendor_check.proxy import FaultRule

NAME = "가상테스트상사"
BZNO = "0000000011"
CORP_CODE = "09000011"

CALLS = {
    "fsc": ("search_corp", {"corp_name": NAME}),
    "nts": ("business_status", {"bzno": BZNO}),
    "dart": ("dart_company", {"corp_code": CORP_CODE}),
}


@pytest.mark.parametrize("agency", ["fsc", "nts", "dart"])
@pytest.mark.parametrize(
    ("kind", "expected"),
    [("timeout", "timeout"), ("http_5xx", "http_5xx"), ("error_200", "gateway"),
     ("quota", "quota"), ("malformed", "malformed"), ("partial", "partial")],
)
async def test_strict_classifies_injected_failures(make_stack, agency, kind, expected):
    async with make_stack("strict") as stack:
        stack.proxy.set_rules([FaultRule(agency, kind, delay_s=1.0)])
        tool, args = CALLS[agency]
        result = await stack.gateway.call(tool, args)
    assert result["ok"] is False
    assert result["error"]["kind"] == expected
    assert result["error"]["agency"] == agency


@pytest.mark.parametrize("agency", ["fsc", "nts", "dart"])
@pytest.mark.parametrize("kind", ["error_200", "quota", "partial", "malformed"])
async def test_lenient_passes_body_level_failures_as_success(make_stack, agency, kind):
    async with make_stack("lenient") as stack:
        stack.proxy.set_rules([FaultRule(agency, kind)])
        tool, args = CALLS[agency]
        result = await stack.gateway.call(tool, args)
    assert result["ok"] is True  # 실패가 정상 결과로 둔갑한다


@pytest.mark.parametrize("kind", ["timeout", "http_5xx"])
async def test_lenient_still_catches_transport_failures(make_stack, kind):
    async with make_stack("lenient") as stack:
        stack.proxy.set_rules([FaultRule("nts", kind, delay_s=1.0)])
        result = await stack.gateway.call(*CALLS["nts"])
    assert result["ok"] is False


async def test_transient_rule_fails_once_then_passes(make_stack):
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("fsc", "http_5xx", times=1)])
        first = await stack.gateway.call(*CALLS["fsc"])
        second = await stack.gateway.call(*CALLS["fsc"])
        stats = stack.proxy.stats()
    assert not first["ok"] and second["ok"]
    assert stats["received"]["fsc"] == 2
    assert stats["forwarded"]["fsc"] == 1


async def test_quota_persists_until_reset(make_stack):
    async with make_stack() as stack:
        stack.proxy.set_rules([FaultRule("nts", "quota", after=1)])
        results = [await stack.gateway.call(*CALLS["nts"]) for _ in range(3)]
        stack.proxy.reset_quota()
        after_reset = await stack.gateway.call(*CALLS["nts"])
    assert [r["ok"] for r in results] == [True, False, False]
    assert after_reset["ok"]


async def test_clean_lookups_return_fixture_values(make_stack, companies):
    async with make_stack() as stack:
        fsc = await stack.gateway.call(*CALLS["fsc"])
        nts = await stack.gateway.call(*CALLS["nts"])
        dart = await stack.gateway.call(*CALLS["dart"])
    assert fsc["data"][0]["crno"] == "0000000000011"
    assert nts["data"]["b_stt_cd"] == "01" and nts["data"]["registered"]
    assert dart["data"]["market"] == "KOSPI"


async def test_unregistered_bzno(make_stack):
    async with make_stack() as stack:
        r = await stack.gateway.call("business_status", {"bzno": "000-00-99999"})
    assert r["ok"] and r["data"]["registered"] is False


async def test_mcp_server_lists_agency_tools(make_stack):
    async with make_stack() as stack:
        tools = await stack.gateway.client.list_tools()
    assert {t.name for t in tools.tools} == {
        "search_corp", "fsc_outline", "business_status", "dart_find_corp", "dart_company"}
