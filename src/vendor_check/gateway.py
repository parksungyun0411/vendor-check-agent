"""에이전트·대조군이 툴을 부르는 통로. 실험에서는 MCP 클라이언트를 거친다."""

from __future__ import annotations

import json
from typing import Protocol

from mcp import Client

from vendor_check.tools import AgencyTools


class ToolGateway(Protocol):
    async def call(self, name: str, args: dict) -> dict: ...


class DirectGateway:
    """MCP 없이 같은 툴 함수를 바로 부른다. 정답 생성처럼 프로토콜이 필요 없는 곳에서 쓴다."""

    def __init__(self, tools: AgencyTools):
        self._fns = tools.as_mapping()

    async def call(self, name: str, args: dict) -> dict:
        return await self._fns[name](**args)


class McpGateway:
    """열린 MCP Client 세션으로 툴을 부르고 구조화 결과를 꺼낸다."""

    def __init__(self, client: Client):
        self.client = client

    async def call(self, name: str, args: dict) -> dict:
        result = await self.client.call_tool(name, args)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return {"ok": False, "error": {"agency": name, "kind": "transport", "message": text,
                                           "code": None, "http_status": None, "retryable": True}}
        data = result.structured_content
        if data is None:
            data = json.loads(result.content[0].text)
        # 반환 타입이 dict 가 아닌 툴은 {"result": ...} 로 감싸진다. 여기 툴은 모두 dict 다.
        return data.get("result", data) if "ok" not in data else data
