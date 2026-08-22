#!/usr/bin/env python3
"""Which Phase 2 requirements have a test, and at which layer. Fully deterministic.

    python3 factory/acceptance/requirement_coverage.py
    python3 factory/acceptance/requirement_coverage.py --json out.json

**This asks a different question from `coverage_report.py`, and the distinction
is worth stating because conflating them wasted a measurement.** Line coverage
asks *which lines executed*, needs the tests to actually run, and for an
end-to-end layer is irreducibly non-deterministic — what it covers depends on
what happened to be in the repository that day.

Requirement coverage asks *does this requirement have at least one test*. That is
a static property of the registries: it reads declarations, runs nothing, touches
no network, needs no credential, and gives the same answer on every machine at
every hour. It is the question worth asking first, because a requirement with
zero tests is invisible to any percentage.

## What counts as covered

Two independent columns, deliberately not merged into one score:

* **acceptance** — a scenario in `factory/acceptance/` drives the real components
  against an in-memory GitHub. Fast, hermetic, runs on every pull request.
* **e2e** — a scenario in `e2e_scenarios.py` drives them against a live
  repository with a live engine.

A requirement wants both. Hermetic coverage alone proved the dependency rule
correct while production rejected every satisfied dependency for two days
(#107); end-to-end alone would be slow, costly and unrunnable in CI. Reporting a
single merged number would hide exactly the case that defect lived in.

**A requirement with neither is an error, not a low score.** It means the factory
promises something nothing checks, and that is the finding this file exists to
surface.

This registry is deliberately Phase 2-only. Phase 3 planning-agent coverage is
measured by `factory/coverage_report.py`; its project acceptance criteria are
not represented here as though they were part of the Phase 2 requirement map.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
for relative in ("..", "../dispatcher", "../runtime", "../gates", "."):
    sys.path.insert(0, os.path.join(HERE, relative))

import phase2_requirements as reqs  # noqa: E402

ROOT = Path(HERE).parents[1]
PHASE4_EVIDENCE = ROOT / "runs" / "phase4" / "evidence.json"

# Every approved Project #212 criterion has named hermetic evidence. A live
# claim is separate: #220 writes the durable ledger after the real run.
PHASE4 = {
    "P4-01": (("test_phase4_worker.py", "test_first_delivery_replay_and_retry_use_one_durable_pr"), False),
    "P4-02": (("test_phase4_worker.py", "test_repository_access_failure_happens_before_any_write"), True),
    "P4-03": (("test_phase4_requirement_coverage.py", "test_shared_credential_boundary_is_explicit"), False),
    "P4-04": (("test_phase4_worker.py", "test_out_of_scope_model_change_creates_no_artifact"), True),
    "P4-05": (("test_phase4_review.py", "test_allowlisted_fresh_input_exact_head_and_replay"), True),
    "P4-06": (("test_phase4_review.py", "test_head_change_during_review_refuses_stale_result_without_a_write"), True),
    "P4-07": (("test_phase4_delivery_loop.py", "test_reject_retry_same_pr_new_head_then_advisory_approval"), True),
    "P4-08": (("test_phase4_delivery_loop.py", "test_reject_retry_same_pr_new_head_then_advisory_approval"), True),
    "P4-09": (("test_phase4_sampling.py", "test_selected_pass_closes_once_and_logs_one_touch"), True),
    "P4-10": (("test_phase4_fixture.py", "test_live_health_interface_returns_authoritative_sha"), False),
    "P4-11": (("test_phase4_delivery_loop.py", "test_reject_retry_same_pr_new_head_then_advisory_approval"), True),
    "P4-12": (("test_phase4_delivery_loop.py", "test_reject_retry_same_pr_new_head_then_advisory_approval"), True),
    "P4-13": (("test_phase4_requirement_coverage.py", "test_every_criterion_has_named_hermetic_evidence"), False),
    "P4-14": (("test_coverage_report.py", "test_phase4_suites_are_explicit_and_risks_are_reported"), False),
    "P4-15": (("test_acceptance.py", "test_every_named_phase_2_behaviour_has_a_scenario"), True),
    "P4-16": (("test_phase4_requirement_coverage.py", "test_phase_boundary_excludes_product_stories"), False),
}


def phase4_evidence(path: Path = PHASE4_EVIDENCE) -> dict:
    if not path.exists():
        return {"criteria": {}, "limitations": {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"criteria": {}, "limitations": {}}
    return value if isinstance(value, dict) else {"criteria": {}, "limitations": {}}


def build_phase4(evidence_path: Path = PHASE4_EVIDENCE) -> dict:
    live = phase4_evidence(evidence_path)
    rows, missing_acceptance, missing_live, stale = [], [], [], []
    for key, ((filename, test_name), requires_live) in PHASE4.items():
        path = Path(HERE) / filename
        present = path.exists() and f"def {test_name}(" in path.read_text()
        if not present:
            stale.append(f"{key}:{filename}::{test_name}")
        recorded = (live.get("criteria") or {}).get(key) == "pass"
        limitation = (live.get("limitations") or {}).get(key, {})
        limited = all(limitation.get(field) for field in
                      ("owner_approved", "why", "substitute_evidence", "residual_risk"))
        if not present: missing_acceptance.append(key)
        if requires_live and not (recorded or limited): missing_live.append(key)
        rows.append({"key": key, "acceptance": f"{filename}::{test_name}",
                     "acceptance_present": present, "live_required": requires_live,
                     "live": "pass" if recorded else ("limitation" if limited else None)})
    return {"scope": "Phase 4 Project #212 criteria", "total": 16,
            "requirements": rows, "missing_acceptance": missing_acceptance,
            "missing_live": missing_live, "stale_declarations": stale}


def render_phase4(report: dict) -> str:
    lines = ["Phase 4 requirement ladder — hermetic and live evidence", ""]
    for row in report["requirements"]:
        lines.append(f"  {row['key']}  acceptance={'yes' if row['acceptance_present'] else 'MISSING'}  "
                     f"live={row['live'] or ('MISSING' if row['live_required'] else 'n/a')}  "
                     f"{row['acceptance']}")
    if report["missing_acceptance"]:
        lines.append("\n  MISSING HERMETIC: " + ", ".join(report["missing_acceptance"]))
    if report["missing_live"]:
        lines.append("\n  MISSING LIVE E2E: " + ", ".join(report["missing_live"]))
    return "\n".join(lines)

# Which acceptance scenario proves which requirement. Declared here rather than
# inferred from names: a mapping guessed from a string is a mapping that goes
# quietly wrong when someone renames a scenario.
ACCEPTANCE_MAP: dict[str, tuple[str, ...]] = {
    "dispatch": ("S1 deterministic dispatch",),
    "authorization": ("S2 authorization chain",),
    "trust-boundary": ("S3 trust boundary",),
    "scope-gate": ("S13 scope enforcement", "S14 fail closed"),
    "worker-launch": ("S8 worker selection and launch",),
    "observability": ("S9 execution observability",),
    "completion": ("S10 no-deliverable completion",),
    "review-open": ("S11 PR and merge reconciliation",),
    "review-merged": ("S11 PR and merge reconciliation",),
    "dependencies": ("S4 dependencies",),
    "replay": ("S7 replay and restart",),
    "recovery": ("S6 claim recovery",),
    "failover": ("S12 worker failure and failover",),
    "attempt-limit": ("S5 WIP and attempt limits",),
    "fail-closed": ("S14 fail closed", "S16 contract conformance"),
}


def acceptance_scenarios() -> set[str]:
    import run_acceptance  # noqa: F401 — importing registers every scenario
    from harness import REGISTRY
    return {cls.name for cls in REGISTRY}


def e2e_scenarios() -> dict[str, str]:
    import e2e_scenarios as mod
    covered = {}
    for cls in mod.REGISTRY:
        for key in cls.covers():
            covered[key] = cls.__name__
    return covered


def build() -> dict:
    live_acceptance = acceptance_scenarios()
    live_e2e = e2e_scenarios()

    rows, missing, stale = [], [], []
    for requirement in reqs.REQUIREMENTS:
        named = ACCEPTANCE_MAP.get(requirement.key, ())
        # A declared scenario that no longer exists is drift, and drift that
        # inflates a coverage number is the worst kind.
        for name in named:
            if name not in live_acceptance:
                stale.append((requirement.key, name))
        acceptance = [n for n in named if n in live_acceptance]
        e2e = live_e2e.get(requirement.key)

        rows.append({
            "key": requirement.key,
            "behaviour": requirement.behaviour,
            "reference": requirement.reference,
            "tier": requirement.tier,
            "acceptance": acceptance,
            "e2e": e2e,
            "tests": len(acceptance) + (1 if e2e else 0),
        })
        if not acceptance and not e2e:
            missing.append(requirement.key)

    return {
        "scope": "Phase 2 requirements only",
        "total": len(rows),
        "with_any_test": sum(1 for r in rows if r["tests"]),
        "with_acceptance": sum(1 for r in rows if r["acceptance"]),
        "with_e2e": sum(1 for r in rows if r["e2e"]),
        "untested": missing,
        "stale_declarations": stale,
        "requirements": rows,
    }


def render(report: dict) -> str:
    lines = ["Requirement coverage — does each Phase 2 requirement have a test?", "",
             "  requirement        acceptance  e2e   behaviour", ""]
    for row in report["requirements"]:
        acc = "yes" if row["acceptance"] else "—"
        e2e = "yes" if row["e2e"] else ("n/a" if row["tier"] == reqs.UNREACHABLE else "—")
        lines.append(f"  {row['key']:<18} {acc:<11} {e2e:<5} {row['behaviour']}")

    total = report["total"]
    lines += ["",
              f"  {report['with_any_test']}/{total} requirement(s) have at least one test",
              f"  {report['with_acceptance']}/{total} have an acceptance scenario "
              f"(hermetic, runs in CI)",
              f"  {report['with_e2e']}/{total} have an end-to-end scenario "
              f"(live repository and engine)"]

    for requirement in reqs.REQUIREMENTS:
        if requirement.tier == reqs.UNREACHABLE:
            lines += ["",
                      f"  `{requirement.key}` has no end-to-end scenario and cannot have one:",
                      f"    {requirement.note}"]

    if report["stale_declarations"]:
        lines += ["", "  STALE: a declared scenario no longer exists — the mapping is "
                      "inflating coverage:"]
        for key, name in report["stale_declarations"]:
            lines.append(f"    {key} -> {name!r}")
    if report["untested"]:
        lines += ["", f"  UNTESTED: {', '.join(report['untested'])}. The factory promises "
                      f"something nothing checks."]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Requirement coverage (deterministic)")
    parser.add_argument("--json", help="also write the report here")
    parser.add_argument("--phase4", action="store_true",
                        help="check Project #212; requires #220 live evidence for wiring claims")
    args = parser.parse_args(argv)

    report = build_phase4() if args.phase4 else build()
    print(render_phase4(report) if args.phase4 else render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"\n  report written to {args.json}")
    failures = ((report["missing_acceptance"] or report["missing_live"] or
                 report["stale_declarations"]) if args.phase4 else
                (report["untested"] or report["stale_declarations"]))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
