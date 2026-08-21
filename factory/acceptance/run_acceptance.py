#!/usr/bin/env python3
"""Run the Phase 2 acceptance suite and write down what it saw.

    python3 factory/acceptance/run_acceptance.py            # everything
    python3 factory/acceptance/run_acceptance.py --scenario S11
    python3 factory/acceptance/run_acceptance.py --quiet     # verdicts only

Exit status is the verdict: zero when every scenario passed, non-zero otherwise.
The report is written to `report.json` beside this file so a CI run can attach
it, because a suite whose result exists only in a terminal is not evidence.

This is the answer to "does Phase 2 work". The 400-odd component tests answer
"is this function correct", which is a different question and not a substitute:
the dependency defect #107 fixed passed every component test in the repository,
because every one of them handed the evaluator a pre-built map instead of making
it go and look.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from harness import REGISTRY, Result, quiet_runlog  # noqa: E402

# Importing a scenario module registers its scenarios, in definition order.
import scenarios_dispatch  # noqa: E402,F401
import scenarios_delivery  # noqa: E402,F401
import scenarios_gates  # noqa: E402,F401

REPORT = os.path.join(HERE, "report.json")


def run(selected: str | None = None) -> list[Result]:
    quiet_runlog()
    results = []
    for cls in REGISTRY:
        if selected and not cls.name.lower().startswith(selected.lower()):
            continue
        results.append(cls().execute())
    return results


def render(results: list[Result], quiet: bool = False) -> str:
    lines = ["Phase 2 acceptance suite", ""]
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        lines.append(f"  {mark}  {result.name:<36} {result.behaviour}")
        if not quiet:
            for observation in result.evidence:
                lines.append(f"          · {observation}")
        if not result.passed:
            lines.append(f"          ✗ {result.error}")
    passed = sum(1 for r in results if r.passed)
    lines.extend(["", f"  {passed}/{len(results)} scenario(s) passed"])
    if passed != len(results):
        lines.append("")
        lines.append("FAIL: Phase 2 is not demonstrated. A scenario that cannot pass "
                     "without changing a frozen contract is an escalation, not a fix.")
    return "\n".join(lines)


def write_report(results: list[Result], path: str = REPORT) -> None:
    payload = {
        "suite": "phase-2-acceptance",
        "scenarios": len(results),
        "passed": sum(1 for r in results if r.passed),
        "results": [r.as_dict() for r in results],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Phase 2 acceptance suite")
    parser.add_argument("--scenario", help="run only scenarios whose name starts with this")
    parser.add_argument("--quiet", action="store_true", help="verdicts only, no evidence")
    parser.add_argument("--report", default=REPORT, help="where to write report.json")
    args = parser.parse_args(argv)

    results = run(args.scenario)
    if not results:
        print(f"no scenario matches {args.scenario!r}")
        return 1
    print(render(results, args.quiet))
    write_report(results, args.report)
    print(f"\nreport written to {args.report}")
    return 0 if all(r.passed for r in results) else 1


def guarded_main(argv: list[str]) -> int:
    """A suite that crashes must not read as a suite that passed."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — deliberately total
        import traceback
        print(f"acceptance suite — FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main(sys.argv[1:]))
