"""환경 변수 설정. 키는 코드에 두지 않고 .env 또는 셸 환경에서만 읽는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

NTS_DEFAULT_BASE = "https://api.odcloud.kr/api/nts-businessman/v1"
FSC_DEFAULT_BASE = "https://apis.data.go.kr/1160100/service/GetCorpBasicInfoService_V2"
DART_DEFAULT_BASE = "https://opendart.fss.or.kr/api"


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    data_go_kr_key: str = ""
    dart_key: str = ""
    nts_base: str = NTS_DEFAULT_BASE
    fsc_base: str = FSC_DEFAULT_BASE
    dart_base: str = DART_DEFAULT_BASE
    timeout_s: float = 10.0
    # strict: 본문까지 검사해 오류를 잡는다. lenient: HTTP 상태만 본다(raise_for_status 수준).
    detection: str = "strict"
    llm_provider: str = "stub"
    llm_model: str = "claude-opus-5-5"
    # 단가는 사용자가 요금표를 보고 직접 넣는다. 비어 있으면 비용 칸을 비워 둔다.
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None
    state_dir: Path = field(default_factory=lambda: Path(".state"))

    @property
    def checkpoint_db(self) -> Path:
        return self.state_dir / "checkpoints.sqlite"

    @property
    def corpcode_cache(self) -> Path:
        return self.state_dir / "CORPCODE.xml"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        env = os.environ.get
        return cls(
            data_go_kr_key=env("DATA_GO_KR_SERVICE_KEY", ""),
            dart_key=env("OPENDART_API_KEY", ""),
            nts_base=env("NTS_BASE_URL", NTS_DEFAULT_BASE),
            fsc_base=env("FSC_BASE_URL", FSC_DEFAULT_BASE),
            dart_base=env("DART_BASE_URL", DART_DEFAULT_BASE),
            timeout_s=float(env("TOOL_TIMEOUT_S", "10")),
            detection=env("DETECTION_MODE", "strict"),
            llm_provider=env("LLM_PROVIDER", "stub"),
            llm_model=env("LLM_MODEL", "claude-opus-5-5"),
            price_in_per_mtok=_float_or_none(env("LLM_PRICE_INPUT_PER_MTOK")),
            price_out_per_mtok=_float_or_none(env("LLM_PRICE_OUTPUT_PER_MTOK")),
            state_dir=Path(env("STATE_DIR", ".state")),
        )
