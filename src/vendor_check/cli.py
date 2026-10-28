"""거래처 한 곳을 대화형으로 점검한다. 동명 법인이면 터미널에서 고른다.

    uv run python -m vendor_check.cli check "상호명"
    uv run python -m vendor_check.cli resume <thread_id>     # 한도 초과로 멈춘 점검을 이어 간다
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid

import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from mcp import Client

from vendor_check.config import Settings
from vendor_check.gateway import McpGateway
from vendor_check.graph import build_graph, drive
from vendor_check.llm import build_writer
from vendor_check.mcp_server import build_server
from vendor_check.recovery import Strategy
from vendor_check.tools import AgencyTools


def ask_human(query: dict, candidates: list[dict]) -> str:
    print(f"'{query['corp_name']}' 와 이름이 같은 법인이 {len(candidates)}곳입니다.")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c['corp_name']} 대표 {c['ceo_name']} 법인번호 {c['crno']} 사업자 {c['bzno']}")
    while True:
        pick = input("번호를 고르세요: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(candidates):
            return candidates[int(pick) - 1]["crno"]


async def _main(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    async with (
        httpx.AsyncClient() as http,
        Client(build_server(AgencyTools(settings, http))) as client,
        AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver,
    ):
        graph = build_graph(McpGateway(client), build_writer(settings),
                            Strategy(args.strategy), checkpointer=saver)
        if args.command == "check":
            thread_id = f"cli-{uuid.uuid4().hex[:8]}"
            out = await drive(graph, thread_id, ask_human, query={"corp_name": args.target})
        else:
            thread_id = args.target
            out = await drive(graph, thread_id, ask_human, resume_quota=True)
    if out["status"] == "interrupted":
        print(f"한도 초과로 멈췄습니다. 내일 이어서: uv run python -m vendor_check.cli resume {thread_id}")
    print(json.dumps({k: out.get(k) for k in ("status", "report", "summary")},
                     ensure_ascii=False, indent=1))


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["check", "resume"])
    p.add_argument("target", help="check: 상호명, resume: thread_id")
    p.add_argument("--strategy", default="checkpoint", choices=[s.value for s in Strategy])
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
