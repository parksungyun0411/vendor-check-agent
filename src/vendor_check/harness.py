"""툴 → 실패 주입 프록시 → 업스트림 을 한 프로세스 안에서 묶는다.

실험 러너와 테스트가 같은 배선을 쓴다. 업스트림은 실제 기관 API(실측) 또는 가짜 업스트림(테스트).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import httpx
from mcp import Client

from vendor_check.config import Settings
from vendor_check.gateway import McpGateway
from vendor_check.mcp_server import build_server
from vendor_check.proxy import AGENCIES, FaultProxy
from vendor_check.tools import AgencyTools

PROXY_ORIGIN = "http://fault-proxy"


@dataclass
class Stack:
    proxy: FaultProxy
    tools: AgencyTools
    gateway: McpGateway
    settings: Settings


@asynccontextmanager
async def open_stack(settings: Settings, upstreams: dict[str, str],
                     upstream_transport: httpx.AsyncBaseTransport | None = None
                     ) -> AsyncIterator[Stack]:
    upstream_http = httpx.AsyncClient(transport=upstream_transport, timeout=60)
    proxy = FaultProxy(upstreams=upstreams, http=upstream_http)
    tool_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app()))
    routed = replace(settings, **{f"{a}_base": f"{PROXY_ORIGIN}/{a}" for a in AGENCIES})
    tools = AgencyTools(routed, tool_http)
    try:
        async with Client(build_server(tools)) as client:
            yield Stack(proxy, tools, McpGateway(client), routed)
    finally:
        await tool_http.aclose()
        await upstream_http.aclose()
