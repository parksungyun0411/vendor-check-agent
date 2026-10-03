"""툴 실패 분류.

실패는 예외로 MCP 경계를 넘기지 않고 데이터(dict)로 넘긴다. 그래야 에이전트와 대조군이
같은 분류를 보고 복구 전략을 고를 수 있다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    HTTP_5XX = "http_5xx"
    HTTP_4XX = "http_4xx"
    QUOTA = "quota"  # 일일 한도 초과. 재시도해도 소용없고 다음 날 재개해야 한다
    RATE_LIMIT = "rate_limit"  # 초당 한도 초과. 잠시 뒤 재시도하면 풀린다
    AUTH = "auth"
    GATEWAY = "gateway"  # 게이트웨이/기관 내부 오류 코드가 본문에 담겨 온 경우
    INVALID_REQUEST = "invalid_request"
    MAINTENANCE = "maintenance"
    MALFORMED = "malformed"  # 본문을 파싱할 수 없음
    PARTIAL = "partial"  # 파싱은 되지만 필수 필드가 빠짐


RETRYABLE = {
    FailureKind.TIMEOUT,
    FailureKind.TRANSPORT,
    FailureKind.HTTP_5XX,
    FailureKind.RATE_LIMIT,
    FailureKind.GATEWAY,
    FailureKind.MALFORMED,
    FailureKind.PARTIAL,
}


@dataclass
class ToolError:
    agency: str
    kind: FailureKind
    message: str = ""
    code: str | None = None
    http_status: int | None = None

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = str(self.kind)
        d["retryable"] = self.retryable
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ToolError:
        return cls(
            agency=d["agency"],
            kind=FailureKind(d["kind"]),
            message=d.get("message", ""),
            code=d.get("code"),
            http_status=d.get("http_status"),
        )


class ToolFailure(Exception):
    def __init__(self, error: ToolError):
        super().__init__(f"{error.agency}:{error.kind}:{error.code} {error.message}")
        self.error = error
