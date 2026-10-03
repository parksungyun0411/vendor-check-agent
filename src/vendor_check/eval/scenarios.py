"""실패 주입 시나리오. 회사 한 곳을 점검할 때마다 규칙을 새로 건다."""

from __future__ import annotations

from dataclasses import dataclass

from vendor_check.proxy import AGENCIES, FaultRule

TRANSIENT_KINDS = ("timeout", "http_5xx", "error_200", "malformed", "partial")


@dataclass(frozen=True)
class Scenario:
    name: str
    agency: str | None
    kind: str | None
    mode: str | None  # transient | persistent | quota

    def rules(self, timeout_s: float) -> list[FaultRule]:
        if self.kind is None:
            return []
        if self.kind == "quota":
            return [FaultRule(self.agency, "quota")]
        times = 1 if self.mode == "transient" else None
        return [FaultRule(self.agency, self.kind, times=times, delay_s=timeout_s * 1.5)]


def all_scenarios() -> list[Scenario]:
    out = [Scenario("clean", None, None, None)]
    for agency in AGENCIES:
        for kind in TRANSIENT_KINDS:
            for mode in ("transient", "persistent"):
                out.append(Scenario(f"{agency}-{kind}-{mode}", agency, kind, mode))
        out.append(Scenario(f"{agency}-quota", agency, "quota", "quota"))
    return out


def select(names: list[str] | None) -> list[Scenario]:
    scenarios = all_scenarios()
    if not names:
        return scenarios
    wanted = set(names)
    picked = [s for s in scenarios if s.name in wanted]
    unknown = wanted - {s.name for s in picked}
    if unknown:
        raise ValueError(f"unknown scenarios: {sorted(unknown)}")
    return picked
