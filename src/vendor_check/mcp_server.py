"""기관별 툴을 MCP 서버로 노출한다 (MCP Python SDK 2.x, MCPServer).

실패 주입 프록시를 끼우려면 NTS_BASE_URL·FSC_BASE_URL·DART_BASE_URL 을 프록시 주소로 바꾼다.
예) NTS_BASE_URL=http://127.0.0.1:8787/nts
"""

from __future__ import annotations

import httpx
from mcp.server.mcpserver import MCPServer

from vendor_check.config import Settings
from vendor_check.tools import TOOL_NAMES, AgencyTools


def build_server(tools: AgencyTools) -> MCPServer:
    server = MCPServer(
        "vendor-check-agency-tools",
        instructions="국세청·금융위·OpenDART 조회 툴. 실패는 ok=false 와 error.kind 로 돌려준다.",
    )
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        server.add_tool(fn, name=name, description=fn.__doc__, structured_output=True)
    return server


def main() -> None:
    settings = Settings.from_env()
    tools = AgencyTools(settings, httpx.AsyncClient())
    build_server(tools).run("stdio")


if __name__ == "__main__":
    main()
