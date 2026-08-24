#!/usr/bin/env python3
"""Coverage by test layer — the same numbers on every run, or none at all.

    python3 factory/coverage_report.py                 # measure and report
    python3 factory/coverage_report.py --json out.json # machine-readable too
    python3 factory/coverage_report.py --check         # prove it is deterministic

Answers three questions the raw percentage cannot:

1. **What is covered** — per production module, line and branch.
2. **What each layer covers alone** — unit, integration, acceptance.
3. **What each layer uniquely contributes** — delete it, how far does coverage
   fall? That last one is the useful number: a layer that adds nothing to reach
   is re-walking paths another layer already covers.

## Determinism is the point, and it is not free

A coverage number that drifts between runs is worse than no number, because it
invites arguing with the measurement instead of the code. Everything below is a
threat this script controls rather than hopes about — each was observed, not
imagined:

* **Environment leakage.** `FACTORY_WORKER_*` in the caller's shell routes the
  runtime down a different path, changing which lines execute. Stripped.
* **Credentials.** `GITHUB_TOKEN` present means a test that slips its mock
  reaches the network, and coverage then depends on GitHub being up. Stripped —
  which also makes an escaped mock fail loudly instead of quietly passing.
* **Stale data files.** `coverage` appends by default; a leftover `.coverage`
  from a previous run silently inflates every figure. Deleted first.
* **Hash seed.** Set ordering can change which branch of a comparison runs.
  `PYTHONHASHSEED=0`.
* **Stray `__pycache__`.** An empty package directory left by a previous
  checkout makes `unittest discover` report "NO TESTS RAN" and exit non-zero,
  which reads as a failing suite. Observed for real while measuring this
  repository's history. Cleaned before each run.
* **Clock and locale.** `TZ=UTC`, `LC_ALL=C` — the runtime writes timestamps.
* **Discovery order.** Suites run in a fixed declared order, never a glob's.

`--check` runs the whole measurement twice in separate processes and diffs the
results. If anything above is missed, that flag is how it gets caught.

## Layer membership is asserted, not guessed

`INTEGRATION` and `ACCEPTANCE` are named explicitly; unit is the remainder. A
test file matching none of the rules is an **error**, not a silent omission — a
new test file must be classified by a person who knows what it tests, because a
misfiled test quietly moves a layer's number without changing a line of code.

## What this deliberately does not do

**It never fails a build on a threshold.** Coverage is a measurement, not a
gate. A required check with a coverage floor is a number the agent under test
can raise by writing shallow tests — exactly the class of self-certifying
control `state-schema.md` §9.14 rules out. Report it, read it, decide with it.

Standard library only, except `coverage` itself, which is optional: without it
this prints how to get it and exits `3`, never a wrong number.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORY = ROOT / "factory"

# Suites in fixed order. Never derived from a glob: a new directory should be
# added here deliberately, so that "what we measure" is a decision on the record.
SUITES = ("gates", "dispatcher", "runtime", "touchlog", "acceptance", "agents/planning",
          "agents/worker", "agents/review")

# Layer membership, by test file. Anything not named here is unit.
INTEGRATION = {
    "runtime/test_lifecycle_e2e.py",   # real dispatcher + poller + workers + completion
    "runtime/test_poller.py",          # the runtime loop against real worker launches
    "agents/planning/test_e2e.py",     # both planning altitudes + durable revision replay
}
ACCEPTANCE = {
    "acceptance/test_acceptance.py",   # the 16 Phase 2 scenarios
    "acceptance/test_rung1_report.py", # Phase 5 deterministic KPI measurement
    "acceptance/test_rung1_live.py",   # Phase 5 black-box harness contract
    "acceptance/test_phase4_worker.py",
    "acceptance/test_phase4_review.py",
    "acceptance/test_phase4_sampling.py",
    "acceptance/test_phase4_fixture.py",
    "acceptance/test_phase4_delivery_loop.py",
    "acceptance/test_phase4_requirement_coverage.py",
    "acceptance/test_phase4_live.py",
    "acceptance/test_phase4_real_harness.py",
    "acceptance/test_coverage_report.py",
    "acceptance/test_acceptance_touch.py",
    "acceptance/test_acceptance_touch_live.py",
    "acceptance/test_acceptance_touch_requirements.py",
    "acceptance/test_product_definition.py",
    "acceptance/test_reviewer_real.py",
    "acceptance/test_two_story_real.py",
    "acceptance/test_e2e_doctor.py",
    "acceptance/test_factory_monitor.py",
}
LAYERS = ("unit", "integration", "acceptance")

# Planning is new in Phase 3 and intentionally explicit: adding a planning test
# requires deciding whether it is a unit test or the hermetic integration layer.
PLANNING_UNIT = {
    "agents/planning/test_artifacts.py",
    "agents/planning/test_contract.py",
    "agents/planning/test_invoke.py",
    "agents/planning/test_run_wrapper.py",
}

PHASE4_UNIT = {
    "runtime/test_phase4_shared_credential.py", "runtime/test_sampling.py",
    "runtime/test_review_route.py", "agents/worker/test_invoke.py",
    "agents/review/test_invoke.py",
    "runtime/test_observability.py", "runtime/test_runlog.py",
}
PHASE4_MODULES = ("factory/agents/worker/invoke.py", "factory/agents/review/invoke.py",
                  "factory/runtime/sampling.py", "factory/runtime/review_route.py",
                  "factory/runtime/review_link.py", "factory/runtime/poller.py")
UNCOVERED_RISKS = (
    "Worker and reviewer share one GitHub principal until Project #221.",
    "Live GitHub, engine, merge, deployment, and sampling wiring is evidenced only by the "
    "separate Story #220 run, never by this deterministic percentage.",
    "Line coverage cannot prove semantic correctness or independent authorization.",
)

# The fourth layer, and the reason it is not in `LAYERS`.
#
# `e2e.py` runs against a live repository with a live engine, so what it covers
# depends on what happens to be in the repository that day: dispatch a story and
# the dispatch path is covered; run when nothing is eligible and it is not. Same
# code, different number, no bug. That is irreducibly non-deterministic, and this
# tool's entire promise is that two runs agree.
#
# So it is measured **separately, on request**, reported apart from the
# deterministic figure, and excluded from `--check`. Folding it into the total
# would quietly destroy the property the rest of the file exists to hold.
#
# It is here rather than left to ad-hoc shell commands because that is the defect
# this tool was written to fix — twice now, the coverage of this repository has
# been measured by hand and the numbers lived only in a transcript.
E2E = "acceptance/e2e.py"

# Production code is what a coverage number is about. Test files cover
# themselves trivially and including them flatters the total by ~7 points.
OMIT = "*/test_*.py,factory/acceptance/*,factory/coverage_report.py"

E2E_ENV = ("GITHUB_TOKEN", "GH_TOKEN")

EXIT_OK, EXIT_TEST_FAILURE, EXIT_NO_COVERAGE, EXIT_NONDETERMINISTIC = 0, 1, 3, 4


def clean_environment() -> dict:
    """The environment every measurement runs in. See the module docstring."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("FACTORY_") and key not in ("GITHUB_TOKEN", "GH_TOKEN")
    }
    env.update({
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "LC_ALL": "C",
        "FACTORY_RUNTIME_LOG_STDERR": "0",
        # OS thread scheduling is not a reproducible line-coverage input. The
        # heartbeat behaviour has an explicit test which opts it back in.
        "FACTORY_HEARTBEATS": "0",
    })
    return env


def purge_pycache() -> None:
    """Remove stray `__pycache__`, which can make an empty directory look like a
    package and turn `NO TESTS RAN` into a failing suite."""
    for path in FACTORY.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


def coverage_available(python: str) -> bool:
    return subprocess.run([python, "-c", "import coverage"],
                          capture_output=True).returncode == 0


def test_files() -> list[str]:
    """Every discoverable test file, relative to `factory/`, in sorted order."""
    return sorted(
        str(path.relative_to(FACTORY))
        for suite in SUITES
        for path in (FACTORY / suite).glob("test_*.py")
    )


def layer_of(relative: str) -> str:
    if relative in ACCEPTANCE:
        return "acceptance"
    if relative in INTEGRATION:
        return "integration"
    return "unit"


def classify() -> dict[str, list[str]]:
    """Group test files by layer, asserting every file is accounted for."""
    found = test_files()
    if not found:
        raise SystemExit("no test files found — is this the right repository?")

    for declared in sorted(INTEGRATION | ACCEPTANCE):
        if declared not in found:
            raise SystemExit(
                f"{declared} is declared as a layer member but does not exist. "
                f"A stale declaration silently shrinks a layer; fix the list.")

    planning = {path for path in found if path.startswith("agents/planning/")}
    declared_planning = PLANNING_UNIT | {
        path for path in INTEGRATION if path.startswith("agents/planning/")
    }
    if planning != declared_planning:
        missing = sorted(planning - declared_planning)
        stale = sorted(declared_planning - planning)
        raise SystemExit(
            "planning tests must be explicitly classified; "
            f"unclassified={missing or 'none'}, stale={stale or 'none'}")

    phase4 = {path for path in found if ("phase4" in path or path.startswith("agents/worker/")
                                         or path.startswith("agents/review/")
                                         or path == "runtime/test_sampling.py"
                                         or path == "runtime/test_review_route.py"
                                         or path in {"runtime/test_observability.py",
                                                     "runtime/test_runlog.py"})}
    declared_phase4 = PHASE4_UNIT | {path for path in INTEGRATION | ACCEPTANCE
                                     if "phase4" in path or path.startswith("agents/worker/")
                                     or path.startswith("agents/review/")
                                     or path in {"runtime/test_observability.py",
                                                 "runtime/test_runlog.py"}}
    if phase4 != declared_phase4:
        raise SystemExit(f"Phase 4 tests must be explicitly classified; "
                         f"unclassified={sorted(phase4-declared_phase4) or 'none'}, "
                         f"stale={sorted(declared_phase4-phase4) or 'none'}")

    grouped: dict[str, list[str]] = {layer: [] for layer in LAYERS}
    for relative in found:
        grouped[layer_of(relative)].append(relative)
    return grouped


def run_layer(python: str, data_file: Path, files: list[str], env: dict) -> bool:
    """Measure one layer. Returns True if every test in it passed."""
    data_file.unlink(missing_ok=True)
    passed = True
    for relative in files:
        relative_path = Path(relative)
        start = FACTORY / relative_path.parent
        name = relative_path.name
        result = subprocess.run(
            [python, "-m", "coverage", "run", "-a", "--source=factory", "--branch",
             "-m", "unittest", "discover", "-s", str(start), "-p", name],
            cwd=ROOT, capture_output=True, text=True,
            env={**env, "COVERAGE_FILE": str(data_file)})
        if result.returncode != 0:
            passed = False
            print(f"  ! {relative} did not pass — the coverage below is measured "
                  f"against failing tests and should not be read as a baseline",
                  file=sys.stderr)
    return passed


def report(python: str, data_file: Path, env: dict) -> dict:
    """Parse `coverage json` into per-module and total figures."""
    result = subprocess.run(
        [python, "-m", "coverage", "json", "-o", "-", "--omit", OMIT],
        cwd=ROOT, capture_output=True, text=True,
        env={**env, "COVERAGE_FILE": str(data_file)})
    if result.returncode != 0:
        return {"total": 0.0, "modules": {}}
    data = json.loads(result.stdout)
    modules = {
        name: round(values["summary"]["percent_covered"], 1)
        for name, values in sorted(data["files"].items())
    }
    return {"total": round(data["totals"]["percent_covered"], 1), "modules": modules}


def combine(python: str, sources: list[Path], target: Path, env: dict) -> None:
    """Merge layer data files into `target`, leaving the sources intact.

    Two details are easy to get wrong and both fail *quietly*, producing 0.0%
    rather than an error. `coverage combine` **consumes** the files it merges, so
    the sources are copied first — a layer file eaten by one combine would make
    every later one wrong. And it discovers those copies by globbing
    `<basename of COVERAGE_FILE>.*`, not `.coverage.*`, so the copies must be
    named after the target and sit beside it.
    """
    target.unlink(missing_ok=True)
    copies = []
    for index, source in enumerate(sources):
        copy = target.parent / f"{target.name}.{index}"
        shutil.copy(source, copy)
        copies.append(copy)
    result = subprocess.run([python, "-m", "coverage", "combine"],
                            cwd=ROOT, capture_output=True, text=True,
                            env={**env, "COVERAGE_FILE": str(target)})
    for copy in copies:
        copy.unlink(missing_ok=True)
    if not target.exists():
        raise RuntimeError(
            f"coverage combine produced no data ({result.stdout.strip() or 'no output'}). "
            f"Reporting a wrong number here would be worse than stopping.")


def run_e2e(python: str, data_file: Path, target: dict, env: dict) -> tuple[bool, str]:
    """Measure the end-to-end suite. Writes to a real repository — see `E2E`.

    The environment is *not* the sanitised one: E2E needs the credential the
    other layers are deliberately stripped of, and needs whatever
    `FACTORY_WORKER_*` configuration the operator actually runs with, because
    covering a worker path means launching a real worker.
    """
    data_file.unlink(missing_ok=True)
    live = {**os.environ, "COVERAGE_FILE": str(data_file),
            "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [python, "-m", "coverage", "run", "--source=factory", "--branch",
         str(FACTORY / "acceptance" / "e2e.py"),
         "--repo", target["repo"], "--commitment", str(target["commitment"]),
         "--project", str(target["project"])],
        cwd=ROOT, capture_output=True, text=True, env=live)
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    summary = next((line.strip() for line in reversed(lines)
                    if "check(s) passed" in line or "ABORTED" in line), "no report")
    # Keep the failures themselves, not merely the count. The first version
    # reported "46/47 check(s) passed" and discarded which one — a number that
    # tells you something is wrong and refuses to say what, which is the least
    # useful shape a failure report can take.
    failures = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith("FAIL"):
            failures.append(line.strip())
            if index + 1 < len(lines) and lines[index + 1].lstrip().startswith("·"):
                failures.append(lines[index + 1].strip())
    return result.returncode == 0, summary, failures


def measure(python: str, workdir: Path, e2e: dict | None = None) -> dict:
    """The deterministic measurement. Pure with respect to the repository — reads only.

    `e2e` is the exception and is kept structurally apart for that reason: it is
    reported beside the figure, never inside it.
    """
    env = clean_environment()
    purge_pycache()
    workdir.mkdir(parents=True, exist_ok=True)
    # Observability defaults to a long-lived production directory. Whether a
    # 5 MiB stream happens to rotate during this measurement is external state
    # and changes line coverage. Give every measurement a clean private stream.
    observation_dir = workdir / "observability"
    shutil.rmtree(observation_dir, ignore_errors=True)
    env["FACTORY_RUN_DIR"] = str(observation_dir)
    grouped = classify()

    per_layer, data_files, failures = {}, {}, []
    for layer in LAYERS:
        data_file = workdir / f"data.{layer}"
        if not run_layer(python, data_file, grouped[layer], env):
            failures.append(layer)
        data_files[layer] = data_file
        per_layer[layer] = report(python, data_file, env)

    combined_file = workdir / "data.combined"
    combine(python, list(data_files.values()), combined_file, env)
    combined = report(python, combined_file, env)

    # What each layer uniquely contributes: remove it, remeasure, take the drop.
    unique = {}
    for layer in LAYERS:
        others = [data_files[other] for other in LAYERS if other != layer]
        without_file = workdir / f"data.without-{layer}"
        combine(python, others, without_file, env)
        without = report(python, without_file, env)["total"]
        unique[layer] = {
            "without": without,
            "contribution": round(combined["total"] - without, 1),
        }

    result = {
        "tool": "factory/coverage_report.py",
        "scope": "production code only (tests omitted)",
        "layers": {layer: {"files": grouped[layer], "tests": len(grouped[layer]),
                           **per_layer[layer]} for layer in LAYERS},
        "unique_contribution": unique,
        "combined": combined,
        "failing_layers": failures,
        "phase4_modules": {name: combined["modules"].get(name, 0.0)
                           for name in PHASE4_MODULES},
        "uncovered_risks": list(UNCOVERED_RISKS),
    }

    if e2e is not None:
        data_file = workdir / "data.e2e"
        passed, summary, failures = run_e2e(python, data_file, e2e, env)
        if not data_file.exists():
            result["e2e"] = {"deterministic": False, "passed": passed,
                             "summary": summary, "failures": failures,
                             "total": 0.0, "modules": {},
                             "combined_with_e2e": combined["total"], "adds": 0.0}
            return result

        measured = report(python, data_file, env)
        with_e2e_file = workdir / "data.combined-with-e2e"
        combine(python, [combined_file, data_file], with_e2e_file, env)
        with_e2e = report(python, with_e2e_file, env)["total"]
        result["e2e"] = {
            "deterministic": False,
            "passed": passed,
            "summary": summary,
            "failures": failures,
            "total": measured["total"],
            "modules": measured["modules"],
            "combined_with_e2e": with_e2e,
            "adds": round(with_e2e - combined["total"], 1),
        }

    return result


def render(result: dict) -> str:
    lines = ["Coverage by test layer — production code, line and branch", ""]

    lines.append("  Per layer, measured alone")
    for layer in LAYERS:
        entry = result["layers"][layer]
        lines.append(f"    {layer:<12} {entry['total']:>5.1f}%   "
                     f"{entry['tests']} test file(s)")
    lines.append(f"    {'combined':<12} {result['combined']['total']:>5.1f}%")
    lines.append("")

    lines.append("  Phase 4 modules")
    for name, percent in result["phase4_modules"].items():
        lines.append(f"    {percent:>5.1f}%  {name}")
    lines.append("")

    lines.append("  Named uncovered risks")
    lines.extend(f"    - {risk}" for risk in result["uncovered_risks"])
    lines.append("")

    lines.append("  Unique contribution — remove the layer, where does it land")
    for layer in LAYERS:
        entry = result["unique_contribution"][layer]
        lines.append(f"    without {layer:<12} {entry['without']:>5.1f}%   "
                     f"({entry['contribution']:+.1f} pts)")
    lines.append("")

    lines.append("  Per module, combined")
    modules = result["combined"]["modules"]
    for name, percent in sorted(modules.items(), key=lambda item: item[1]):
        lines.append(f"    {percent:>5.1f}%  {name}")
    lines.append("")

    if "e2e" in result:
        e2e = result["e2e"]
        lines.append("  End-to-end — measured separately, and NOT deterministic")
        lines.append(f"    e2e alone     {e2e['total']:>5.1f}%   ({e2e['summary']})")
        lines.append(f"    combined      {e2e['combined_with_e2e']:>5.1f}%   "
                     f"({e2e['adds']:+.1f} pts over the deterministic figure)")
        lines.append("    What it covers depends on what was in the repository when it")
        lines.append("    ran, so it is reported beside the figure above and never inside")
        lines.append("    it. --check does not cover this layer and cannot.")
        if not e2e["passed"]:
            lines.append("    FAILED — the checks that did not pass:")
            for failure in e2e.get("failures", []) or ["    (no detail captured)"]:
                lines.append(f"      {failure}")
        lines.append("")

    if result["failing_layers"]:
        lines.append(f"  WARNING: tests failed in {', '.join(result['failing_layers'])}. "
                     f"These numbers are measured against failing tests.")
    lines.append("  Coverage is reported, never enforced: a threshold in a required "
                 "check is a number the")
    lines.append("  code under test can raise with shallow tests (§9.14).")
    return "\n".join(lines)


def comparable(result: dict) -> dict:
    """The parts that must be identical across runs. Excludes nothing today —
    stated explicitly so that adding an exemption is a visible decision."""
    return {"layers": result["layers"], "combined": result["combined"],
            "unique_contribution": result["unique_contribution"],
            "phase4_modules": result["phase4_modules"],
            "uncovered_risks": result["uncovered_risks"]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Coverage by test layer")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter with `coverage` importable")
    parser.add_argument("--json", help="also write the report to this path")
    parser.add_argument("--check", action="store_true",
                        help="measure twice in separate processes and diff")
    parser.add_argument("--workdir", default=None,
                        help="where coverage data files live (default: a temp dir)")
    parser.add_argument("--with-e2e", nargs=3, metavar=("REPO", "COMMITMENT", "PROJECT"),
                        help="also measure the end-to-end suite. WRITES TO A REAL "
                             "REPOSITORY and spends real engine invocations; the result "
                             "is non-deterministic and is reported separately")
    args = parser.parse_args(argv)

    e2e = None
    if args.with_e2e:
        repo, commitment, project = args.with_e2e
        if not any(os.environ.get(key) for key in E2E_ENV):
            print("--with-e2e needs GITHUB_TOKEN or GH_TOKEN: the end-to-end suite "
                  "writes to a real repository.", file=sys.stderr)
            return EXIT_NO_COVERAGE
        e2e = {"repo": repo, "commitment": int(commitment), "project": int(project)}
        if args.check:
            parser.error("--check and --with-e2e are contradictory: the end-to-end "
                         "layer is not deterministic, which is the whole reason it is "
                         "reported apart from the figure --check verifies")

    if not coverage_available(args.python):
        print("coverage.py is not importable by "
              f"{args.python}.\n\n"
              "This repository has no dependencies by design, so the tool is not\n"
              "vendored. Create a throwaway environment outside the repository:\n\n"
              "    python3 -m venv /tmp/factory-cov\n"
              "    /tmp/factory-cov/bin/pip install coverage\n"
              "    python3 factory/coverage_report.py --python /tmp/factory-cov/bin/python\n\n"
              "Reporting no number is correct here. A cruder measurement presented\n"
              "as the same figure would be worse than none.", file=sys.stderr)
        return EXIT_NO_COVERAGE

    import tempfile
    with tempfile.TemporaryDirectory(prefix="factory-coverage-") as tmp:
        workdir = Path(args.workdir) if args.workdir else Path(tmp)
        result = measure(args.python, workdir / "first", e2e)
        print(render(result))

        if args.check:
            second = measure(args.python, workdir / "second")
            if comparable(result) != comparable(second):
                print("\nNON-DETERMINISTIC: two runs disagreed.", file=sys.stderr)
                print(json.dumps({"first": comparable(result),
                                  "second": comparable(second)},
                                 indent=2, sort_keys=True), file=sys.stderr)
                return EXIT_NONDETERMINISTIC
            print("\n  --check: two independent runs produced identical figures.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(f"\n  report written to {args.json}")

    if result["failing_layers"]:
        return EXIT_TEST_FAILURE
    if "e2e" in result and not result["e2e"]["passed"]:
        # Non-deterministic does not mean advisory. A run that failed is a run
        # that failed, and exiting 0 beside a printed FAILED is how a failure
        # becomes a number nobody acts on.
        return EXIT_TEST_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
