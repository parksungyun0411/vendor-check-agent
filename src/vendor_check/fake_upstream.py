"""오프라인 테스트용 가짜 기관 API (SYNTHETIC).

실제 API의 경로·필드명·응답 봉투를 흉내 내되 값은 tests/fixtures 의 합성 데이터에서 온다.
봉투 중 확인하지 못한 부분(금융위 JSON 최상위 키 "response", resultCode "00",
corpRegMrktDcdNm 값)은 clients/fsc.py 의 가정을 그대로 따른다. 이 서버가 내는 숫자는
테스트 단언일 뿐 프로젝트 결과가 아니다.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from vendor_check.clients.nts import UNREGISTERED_MSG

MARKET_NAME = {"KOSPI": "유가증권시장", "KOSDAQ": "코스닥", "KONEX": "코넥스", "NONE": "기타"}
CORP_CLS = {"KOSPI": "Y", "KOSDAQ": "K", "KONEX": "N", "NONE": "E"}
B_STT = {"01": "계속사업자", "02": "휴업자", "03": "폐업자"}


def load_companies(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["companies"]


def build_fake_upstream(companies: list[dict]) -> Starlette:
    by_bzno = {c["bzno"]: c for c in companies}
    by_code = {c["corp_code"]: c for c in companies if c.get("corp_code")}

    async def nts_status(request: Request) -> Response:
        b_nos = (await request.json()).get("b_no", [])
        data = []
        for b in b_nos:
            c = by_bzno.get(b)
            if c is None:
                data.append({"b_no": b, "b_stt": "", "b_stt_cd": "", "tax_type": UNREGISTERED_MSG,
                             "tax_type_cd": "", "end_dt": ""})
            else:
                data.append({"b_no": b, "b_stt": B_STT[c["b_stt_cd"]], "b_stt_cd": c["b_stt_cd"],
                             "tax_type": "부가가치세 일반과세자", "tax_type_cd": c["tax_type_cd"],
                             "end_dt": "20250101" if c["b_stt_cd"] == "03" else ""})
        return JSONResponse({"status_code": "OK", "request_cnt": len(b_nos),
                             "match_cnt": len(data), "data": data})

    async def fsc_outline(request: Request) -> Response:
        q = request.query_params
        rows = [c for c in companies
                if (q.get("corpNm") and c["corp_name"] == q["corpNm"])
                or (q.get("crno") and c["crno"] == q["crno"])]
        items = [{"crno": c["crno"], "corpNm": c["corp_name"], "enpRprFnm": c["ceo_name"],
                  "bzno": c["bzno"], "corpRegMrktDcdNm": MARKET_NAME[c["market"]],
                  "fssCorpUnqNo": c.get("corp_code") or "", "fssCorpChgDtm": "20260101000000"}
                 for c in rows]
        return JSONResponse({"response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {"numOfRows": q.get("numOfRows"), "pageNo": "1", "totalCount": len(items),
                     "items": {"item": items}},
        }})

    async def dart_company(request: Request) -> Response:
        c = by_code.get(request.query_params.get("corp_code", ""))
        if c is None:
            return JSONResponse({"status": "013", "message": "조회된 데이타가 없습니다."})
        return JSONResponse({"status": "000", "message": "정상", "corp_name": c["corp_name"],
                             "ceo_nm": c["ceo_name"], "corp_cls": CORP_CLS[c["market"]],
                             "jurir_no": c["crno"], "bizr_no": c["bzno"],
                             "stock_code": "000000" if c["market"] != "NONE" else " "})

    async def dart_corpcode(request: Request) -> Response:
        rows = "".join(
            f"<list><corp_code>{c['corp_code']}</corp_code><corp_name>{c['corp_name']}</corp_name>"
            f"<stock_code> </stock_code><modify_date>20260101</modify_date></list>"
            for c in by_code.values()
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", f'<?xml version="1.0" encoding="UTF-8"?><result>{rows}</result>')
        return Response(buf.getvalue(), media_type="application/zip")

    return Starlette(routes=[
        Route("/nts/status", nts_status, methods=["POST"]),
        Route("/fsc/getCorpOutline_V2", fsc_outline),
        Route("/dart/company.json", dart_company),
        Route("/dart/corpCode.xml", dart_corpcode),
    ])
