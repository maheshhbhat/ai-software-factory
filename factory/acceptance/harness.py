#!/usr/bin/env python3
"""The scenario harness — what a Phase 2 acceptance scenario is.

A scenario is not a unit test. A unit test asks whether a function is correct
against inputs it was handed; a scenario asks whether **the factory does the
thing Phase 2 promised**, by driving a real entry point and reading the durable
state that comes out.

Three rules keep that distinction honest, and each one exists because dropping
it turns the suite back into unit tests wearing a different name:

* **Drive a real entry point.** `poller.poll_once`, `dispatcher.main`,
  `merge_gate.main`, `continuation.run`, `review_link.run`, `humanqueue.run`.
  Never a helper reached around them, and never a re-implementation of a rule
  that already has a home.
* **Assert on durable state.** Labels, open/closed, `state_reason`, comments,
  the timeline — plus stdout and the runlog, which are the factory's only other
  outputs. Not on return values, because a return value is not what the factory
  keeps.
* **Say what the evidence was.** Every scenario records the observations that
  led to its verdict, and the report prints them. A green suite that cannot show
  its working is an assertion, not evidence.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
for relative in ("..", "../dispatcher", "../runtime", "../gates"):
    sys.path.insert(0, os.path.join(HERE, relative))

from capacity_pool.policy import resolved_registry  # noqa: E402
from capacity_pool.state import CapacityState  # noqa: E402


@dataclass
class Result:
    name: str
    behaviour: str
    passed: bool
    evidence: list[str] = field(default_factory=list)
    error: str = ""
    traceback: str = ""

    def as_dict(self) -> dict:
        record = {"scenario": self.name, "behaviour": self.behaviour,
                  "passed": self.passed, "evidence": self.evidence}
        if not self.passed:
            record["error"] = self.error
        return record


class Scenario:
    """One question about Phase 2, and the way to answer it.

    Subclasses set `name` and `behaviour` and implement `run`, raising
    `AssertionError` when the factory does not do what Phase 2 promised.
    """

    name = ""
    behaviour = ""

    def __init__(self):
        self.evidence: list[str] = []

    def note(self, observation: str) -> None:
        """Record an observation that the verdict rests on."""
        self.evidence.append(observation)

    def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- helpers every scenario ends up wanting ----------------------------

    def capture(self, call, *args, **kwargs) -> str:
        """Run something and return its stdout — an output the factory keeps."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            call(*args, **kwargs)
        return buffer.getvalue()

    def execute(self) -> Result:
        previous = os.environ.get("FACTORY_CAPACITY_STATE")
        with tempfile.TemporaryDirectory(prefix="factory-acceptance-capacity-") as temp:
            os.environ["FACTORY_CAPACITY_STATE"] = os.path.join(temp, "state.sqlite")
            state = CapacityState(os.environ["FACTORY_CAPACITY_STATE"], uri=False)
            try:
                for model in resolved_registry():
                    if model.available:
                        state.mark_healthy(model.provider, model.name,
                                           "acceptance-fixture")
                try:
                    self.run()
                except AssertionError as failure:
                    return Result(self.name, self.behaviour, False, self.evidence,
                                  str(failure) or "assertion failed",
                                  traceback.format_exc())
                except Exception as error:  # noqa: BLE001 — crash is a failed scenario
                    return Result(self.name, self.behaviour, False, self.evidence,
                                  f"{type(error).__name__}: {error}",
                                  traceback.format_exc())
                return Result(self.name, self.behaviour, True, self.evidence)
            finally:
                state.close()
                if previous is None:
                    os.environ.pop("FACTORY_CAPACITY_STATE", None)
                else:
                    os.environ["FACTORY_CAPACITY_STATE"] = previous


REGISTRY: list[type[Scenario]] = []


def scenario(cls: type[Scenario]) -> type[Scenario]:
    """Register a scenario. Order of definition is order of execution."""
    assert cls.name, f"{cls.__name__} has no name"
    assert cls.behaviour, f"{cls.name} does not say which behaviour it proves"
    REGISTRY.append(cls)
    return cls


def quiet_runlog() -> None:
    """Keep the runlog out of the report's stderr, without disabling it.

    Scenarios that assert on the runlog read the records it returns; the file
    and the stderr mirror are noise here.
    """
    os.environ["FACTORY_RUNTIME_LOG_STDERR"] = "0"
    os.environ.setdefault(
        "FACTORY_RUNTIME_LOG",
        os.path.join(HERE, "..", "runtime", "logs", "acceptance.jsonl"))
