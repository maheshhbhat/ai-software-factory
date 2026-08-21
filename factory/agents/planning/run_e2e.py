#!/usr/bin/env python3
"""Repeatable Phase 3 E2E suite and criterion coverage report."""

import argparse
import json
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_e2e  # noqa: E402

REQUIREMENTS = {
    "C1 prompt/input contract": "test_contract + test_01",
    "C2 two altitudes": "test_01",
    "C3 story rendered form": "test_01 + independent read-back",
    "C4 bells/digest": "test_01 + independent read-back",
    "C5 bounds/idempotency": "test_02, test_03, test_05",
    "C6 private access failure": "test_04 (hermetic 403/404)",
    "C7 consequential decisions": "prompt contract + live human review",
    "C8 live campaign run": "LIVE — income-portfolio-analyzer#1",
    "C9 live project run": "LIVE — generated product project",
    "C10 signable digest": "HUMAN — Project #186 outcome acceptance",
    "C11 rails/merge gate": "LIVE — delivery PR checks",
}


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", help="optional JSON report path")
    args = parser.parse_args(argv)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_e2e.PlanningE2E)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"passed": result.wasSuccessful(), "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "requirements": REQUIREMENTS}
    print("\nRequirement coverage")
    for requirement, evidence in REQUIREMENTS.items():
        print(f"  {requirement}: {evidence}")
    if args.report:
        pathlib.Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print(f"report written to {args.report}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
