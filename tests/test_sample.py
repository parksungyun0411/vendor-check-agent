"""표본 추출의 층 판정과 할당. 가짜 툴로 돌리며, 여기 나오는 회사·숫자는 합성 값이다."""

import argparse
import random

import pytest

from vendor_check.eval.sample import _alternate, _main, build_pools, draw, fsc_check, prospective

LIVE = {"registered": True, "b_stt_cd": "01"}
CLOSED = {"registered": True, "b_stt_cd": "03"}


def corp(code: str, name: str, stock: str | None = None) -> dict:
    return {"corp_code": code, "corp_name": name, "stock_code": stock}


def test_pools_split_listed_unlisted_and_homonyms():
    corps = [corp("1", "가상상장", "000001"), corp("2", "가상비상장"),
             corp("3", "가상동명"), corp("4", "가상동명")]
    pools = build_pools(corps, random.Random(0))
    assert [c["corp_code"] for c in pools["listed"]] == ["1"]
    assert [c["corp_code"] for c in pools["unlisted"]] == ["2"]
    assert len(pools["dup"]) == 1 and pools["dup"][0]["corp_name"] == "가상동명"


def test_prospective_strata_and_drops():
    listed = {"bzno": "0000000011", "market": "KOSPI"}
    unlisted = {"bzno": "0000000022", "market": "NONE"}
    assert prospective("listed", listed, LIVE) == "listed"
    assert prospective("unlisted", unlisted, LIVE) == "unlisted"
    assert prospective("unlisted", unlisted, CLOSED) == "closed"
    assert prospective("dup", unlisted, CLOSED) == "dup"
    assert prospective("listed", unlisted, LIVE) == "drop:not_listed"
    assert prospective("unlisted", None, None) == "drop:dart_not_found"
    assert prospective("unlisted", {"bzno": None}, None) == "drop:no_bzno"
    assert prospective("unlisted", unlisted, {"registered": False}) == "drop:nts_unregistered"


def test_fsc_check_needs_matching_bzno_and_homonyms_for_dup():
    company = {"bzno": "000-00-00011"}
    one = [{"crno": "A", "corp_name": "가상동명", "bzno": "0000000011"}]
    two = one + [{"crno": "B", "corp_name": "가상동명", "bzno": "0000000099"}]
    assert fsc_check("listed", company, one) is None
    assert fsc_check("listed", company, [{"crno": "B", "bzno": "0000000099"}]) == "drop:fsc_no_match"
    assert fsc_check("listed", company, None) == "drop:fsc_failed"
    assert fsc_check("dup", company, one) == "drop:fsc_not_homonym"
    assert fsc_check("dup", company, two) is None


class FakeTools:
    def __init__(self, companies: dict, status: dict, fsc: dict):
        self.companies, self.status, self.fsc = companies, status, fsc

    async def dart_company(self, code):
        return {"ok": True, "data": self.companies.get(code)}

    async def business_status(self, bzno):
        return {"ok": True, "data": self.status[bzno]}

    async def search_corp(self, name):
        return {"ok": True, "data": self.fsc.get(name, [])}


async def test_draw_fills_quota_and_counts_outcomes():
    corps = [corp("1", "가상상장", "000001"), corp("2", "가상비상장"), corp("3", "가상폐업")]
    companies = {"1": {"bzno": "0000000001", "market": "KOSDAQ"},
                 "2": {"bzno": "0000000002", "market": "NONE"},
                 "3": {"bzno": "0000000003", "market": "NONE"}}
    status = {"0000000001": LIVE, "0000000002": LIVE, "0000000003": CLOSED}
    fsc = {c["corp_name"]: [{"crno": c["corp_code"], "corp_name": c["corp_name"],
                             "bzno": companies[c["corp_code"]]["bzno"]}] for c in corps}
    quota = {"listed": 1, "unlisted": 1, "closed": 1, "dup": 0}
    rows, stats = await draw(FakeTools(companies, status, fsc), corps, random.Random(0), quota, 10, 0)
    assert {r["id"]: r["corp_name"] for r in rows} == {"lst-01": "가상상장", "unl-01": "가상비상장",
                                                       "cls-01": "가상폐업"}
    assert stats["picked"] == {"listed": 1, "unlisted": 1, "closed": 1, "dup": 0}
    assert stats["outcomes"] == {"ok:closed": 1, "ok:listed": 1, "ok:unlisted": 1}


async def test_draw_alternates_pools_so_closed_is_not_from_one_pool():
    corps = [corp("1", "가상상장폐업", "000001"), corp("2", "가상상장", "000002"),
             corp("3", "가상비상장폐업"), corp("4", "가상비상장")]
    companies = {"1": {"bzno": "0000000001", "market": "NONE"},
                 "2": {"bzno": "0000000002", "market": "KOSPI"},
                 "3": {"bzno": "0000000003", "market": "NONE"},
                 "4": {"bzno": "0000000004", "market": "NONE"}}
    status = {"0000000001": CLOSED, "0000000002": LIVE, "0000000003": CLOSED, "0000000004": LIVE}
    fsc = {c["corp_name"]: [{"crno": c["corp_code"], "corp_name": c["corp_name"],
                             "bzno": companies[c["corp_code"]]["bzno"]}] for c in corps}
    quota = {"listed": 1, "unlisted": 1, "closed": 2, "dup": 0}
    _, stats = await draw(FakeTools(companies, status, fsc), corps, random.Random(0), quota, 10, 0)
    assert stats["picked_from_pool"] == {"closed<-listed": 1, "closed<-unlisted": 1,
                                         "listed<-listed": 1, "unlisted<-unlisted": 1}


def test_alternate_interleaves_then_continues_longer_pool():
    out = [(p, c["corp_code"]) for p, c in _alternate([corp("1", "a"), corp("2", "b")],
                                                       [corp("3", "c"), corp("4", "d"), corp("5", "e")])]
    assert out == [("listed", "1"), ("unlisted", "3"), ("listed", "2"), ("unlisted", "4"), ("unlisted", "5")]


async def test_refuses_to_resample_over_existing_runs(tmp_path):
    (tmp_path / "inject.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="다시 뽑지 않습니다"):
        await _main(argparse.Namespace(runs=str(tmp_path)))
