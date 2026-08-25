#!/usr/bin/env python3
"""Provider adapter contract used by the shared executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AttemptResult:
    outcome: str
    output: str = ""
    consumed_budget_units: float | None = None
    mutation_state: str = "none"
    diagnostic: str = ""
    failure_scope: str = "model"

    def __post_init__(self):
        if self.failure_scope not in {"model", "provider"}:
            raise ValueError("failure_scope must be model or provider")

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"


class ProviderAdapter:
    """Small injectable boundary; concrete CLI syntax lives in this package."""

    def __init__(self, provider: str,
                 invoke: Callable[..., AttemptResult],
                 probe: Callable[..., bool] | None = None):
        self.provider = provider
        self._invoke = invoke
        self._probe = probe

    def run(self, *, model: str, effort: str, timeout_seconds: int,
            budget_units: float, payload) -> AttemptResult:
        result = self._invoke(model=model, effort=effort,
                              timeout_seconds=timeout_seconds,
                              budget_units=budget_units, payload=payload)
        if not isinstance(result, AttemptResult):
            raise TypeError("provider adapter returned an invalid attempt result")
        return result

    def health_probe(self, *, model: str, timeout_seconds: int,
                     effort: str = "low") -> bool:
        if self._probe is None:
            return False
        return bool(self._probe(model=model, timeout_seconds=timeout_seconds,
                                effort=effort))
