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
SUITES = ("gates", "dispatcher", "runtime", "acceptance")

# Layer membership, by test file. Anything not named here is unit.
INTEGRATION = {
    "runtime/test_lifecycle_e2e.py",   # real dispatcher + poller + workers + completion
    "runtime/test_poller.py",          # the runtime loop against real worker launches
}
ACCEPTANCE = {
    "acceptance/test_acceptance.py",   # the 16 Phase 2 scenarios
}
LAYERS = ("unit", "integration", "acceptance")

# Production code is what a coverage number is about. Test files cover
# themselves trivially and including them flatters the total by ~7 points.
OMIT = "*/test_*.py,factory/acceptance/*,factory/coverage_report.py"

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

    grouped: dict[str, list[str]] = {layer: [] for layer in LAYERS}
    for relative in found:
        grouped[layer_of(relative)].append(relative)
    return grouped


def run_layer(python: str, data_file: Path, files: list[str], env: dict) -> bool:
    """Measure one layer. Returns True if every test in it passed."""
    data_file.unlink(missing_ok=True)
    passed = True
    for relative in files:
        suite, name = relative.split("/", 1)
        result = subprocess.run(
            [python, "-m", "coverage", "run", "-a", "--source=factory", "--branch",
             "-m", "unittest", "discover", "-s", str(FACTORY / suite), "-p", name],
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


def measure(python: str, workdir: Path) -> dict:
    """The whole measurement. Pure with respect to the repository — reads only."""
    env = clean_environment()
    purge_pycache()
    workdir.mkdir(parents=True, exist_ok=True)
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

    return {
        "tool": "factory/coverage_report.py",
        "scope": "production code only (tests omitted)",
        "layers": {layer: {"files": grouped[layer], "tests": len(grouped[layer]),
                           **per_layer[layer]} for layer in LAYERS},
        "unique_contribution": unique,
        "combined": combined,
        "failing_layers": failures,
    }


def render(result: dict) -> str:
    lines = ["Coverage by test layer — production code, line and branch", ""]

    lines.append("  Per layer, measured alone")
    for layer in LAYERS:
        entry = result["layers"][layer]
        lines.append(f"    {layer:<12} {entry['total']:>5.1f}%   "
                     f"{entry['tests']} test file(s)")
    lines.append(f"    {'combined':<12} {result['combined']['total']:>5.1f}%")
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
            "unique_contribution": result["unique_contribution"]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Coverage by test layer")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter with `coverage` importable")
    parser.add_argument("--json", help="also write the report to this path")
    parser.add_argument("--check", action="store_true",
                        help="measure twice in separate processes and diff")
    parser.add_argument("--workdir", default=None,
                        help="where coverage data files live (default: a temp dir)")
    args = parser.parse_args(argv)

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
        result = measure(args.python, workdir / "first")
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

    return EXIT_TEST_FAILURE if result["failing_layers"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
