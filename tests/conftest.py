from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from vendor_check.config import Settings
from vendor_check.fake_upstream import build_fake_upstream, load_companies
from vendor_check.harness import open_stack

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_companies.json"
FAKE_UPSTREAMS = {"nts": "http://fake/nts", "fsc": "http://fake/fsc", "dart": "http://fake/dart"}


@pytest.fixture
def companies() -> list[dict]:
    return load_companies(FIXTURE)


@pytest.fixture
def make_stack(companies, tmp_path):
    """가짜 업스트림 위에 프록시·툴·MCP 서버를 올린다. detection 을 바꿔 열 수 있다."""

    def _open(detection: str = "strict", timeout_s: float = 0.3):
        settings = Settings(data_go_kr_key="SYNTHETIC", dart_key="SYNTHETIC",
                            timeout_s=timeout_s, detection=detection, state_dir=tmp_path)
        transport = httpx.ASGITransport(app=build_fake_upstream(companies))
        return open_stack(settings, FAKE_UPSTREAMS, upstream_transport=transport)

    return _open


def by_id(companies: list[dict], cid: str) -> dict:
    return next(c for c in companies if c["id"] == cid)
