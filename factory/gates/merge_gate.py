#!/usr/bin/env python3
"""Deterministic merge gate — Phase 2 Increment 2.

Evaluates a pull request against the frozen contracts in
`factory/spec/state-schema.md` §9. Fails closed: any input the gate cannot
obtain or cannot parse is a violation, never a pass.

Trusted-main rule (§9.14, as corrected by #39). The verdict is computed by the
copy of this file already on `main`, never by the copy inside the pull request
being judged. A PR therefore cannot weaken the rules it is evaluated against;
a proposed change takes effect only after it lands. That makes "the gate never
vouches for a modified copy of itself" a structural property rather than a rule
the gate applies to itself — which is why `factory/gates/**` changes no longer
have to be refused outright.

The one thing this cannot protect is the workflow *runner*
(`.github/workflows/merge-gate.yml`): for same-repo `pull_request` events GitHub
executes the workflow file from the PR head, so a PR that rewrites the runner
runs its own rewrite. No check can protect the file that defines the check. That
path is covered by human review and audit only — a convention under the shared
credential, not an enforcement (§9.7). `--mode surface` reports it loudly so a
missing acknowledgement is detectable after the fact.

Trust boundary (§9.14). The gate reads only inputs the agent's credential
cannot fabricate:

  * the diff        — changed paths, from the PR's file list
  * the Scope       — the linked story's `### Scope` section
  * test results    — computed by CI in this run and passed in by the workflow
  * the workflow    — self-modification detected from the diff itself

It deliberately does NOT read labels, `Agent-ID`, comments, review state, or
the PR author. Those are forgeable by anything holding the repository
credential and must never decide a verdict.

Usage:
    merge_gate.py --pr <number> --repo <owner/name> --tests-passed <true|false>
    merge_gate.py --fixture <path>            # offline evaluation, for tests

Exit status is 0 when every check passes and 1 when any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime"))
import observability as obs  # noqa: E402

# The schema major this gate implements. §9.1: pin a major, halt on anything else.
SUPPORTED_SCHEMA_MAJOR = 2

# The enforcement surface, split by whether CI can protect it (#39).
#
# Gate logic is protected by construction: the trusted `main` copy computes the
# verdict, so a change here cannot influence its own evaluation. Advisory only.
GATE_LOGIC_PATTERNS = ("factory/gates/**",)

# The runner defines the check, so no check can protect it. Reported as a loud
# advisory failure on `merge-gate-surface`; never a blocking `merge-gate` verdict,
# because blocking it would make the gate unmodifiable once required.
GATE_RUNNER_PATTERNS = (".github/workflows/merge-gate.yml",)

# An automated delivery worker must never modify the factory that authorizes,
# launches, reviews, or merges that worker. Direct implementation PRs remain
# possible; the automated marker is checked separately in evaluate().
PROTECTED_FACTORY_PATTERNS = (
    "factory/**", ".github/**", ".claude/**",
    "poll.sh", "approve-plan.sh", "live-e2e.sh", "AGENTS.md", "CLAUDE.md",
)
PROTECTED_FACTORY_DIRS = ("factory", ".github", ".claude")
PROTECTED_FACTORY_FILES = (
    "poll.sh", "approve-plan.sh", "live-e2e.sh", "AGENTS.md", "CLAUDE.md",
)
WORKER_ARTIFACT_RE = re.compile(r"<!--\s*worker-artifact:\d+:[^>]+-->")

STORY_LINK_RE = re.compile(r"^Story:\s*#(\d+)\s*$", re.MULTILINE)
SCHEMA_LINE_RE = re.compile(r"^Schema-Version:\s*(\d+)\.(\d+)\.(\d+)\s*$", re.MULTILINE)
SECTION_RE = r"^### {name}\s*$\n(.*?)(?=^### |\Z)"


class Violation:
    """Stable machine-readable violation codes. One per failure class, so each
    can fail independently rather than through a catch-all."""

    LINK_MISSING = "LINK_MISSING"
    LINK_DUPLICATE = "LINK_DUPLICATE"
    STORY_NOT_FOUND = "STORY_NOT_FOUND"
    STORY_WRONG_TYPE = "STORY_WRONG_TYPE"
    SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
    SCHEMA_LEGACY_ARTIFACT = "SCHEMA_LEGACY_ARTIFACT"
    SCOPE_MISSING = "SCOPE_MISSING"
    SCOPE_MALFORMED = "SCOPE_MALFORMED"
    SCOPE_EMPTY = "SCOPE_EMPTY"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    TESTS_FAILED = "TESTS_FAILED"
    NO_CHANGES = "NO_CHANGES"
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    FACTORY_SELF_MODIFICATION_FORBIDDEN = "FACTORY_SELF_MODIFICATION_FORBIDDEN"


@dataclass
class Finding:
    code: str
    detail: str


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def fail(self, code: str, detail: str) -> None:
        self.findings.append(Finding(code, detail))

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]


# --------------------------------------------------------------------------
# Pure parsing and matching — no I/O, fully unit-testable
# --------------------------------------------------------------------------


def parse_section(body: str, name: str) -> str | None:
    """Return the raw text of a `### <name>` section, or None if absent."""
    if body is None:
        return None
    match = re.search(SECTION_RE.format(name=re.escape(name)), body, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def parse_story_link(pr_body: str) -> tuple[int | None, str | None]:
    """§9.5: exactly one canonical `Story: #N` line. Returns (number, error)."""
    if not pr_body:
        return None, Violation.LINK_MISSING
    matches = STORY_LINK_RE.findall(pr_body)
    if not matches:
        return None, Violation.LINK_MISSING
    if len(matches) > 1:
        return None, Violation.LINK_DUPLICATE
    return int(matches[0]), None


def parse_schema_version(text: str) -> int | None:
    """Optional `Schema-Version: X.Y.Z` declaration. Returns the major, or None
    when undeclared (which means 'current', per §9.1)."""
    if not text:
        return None
    match = SCHEMA_LINE_RE.search(text)
    return int(match.group(1)) if match else None


def parse_scope(story_body: str) -> tuple[list[str], str | None]:
    """§9.6: one pattern per line, no bullets, no blank lines, no commentary.

    Returns (patterns, error_code). Fails closed on anything ambiguous.
    """
    raw = parse_section(story_body or "", "Scope")
    if raw is None:
        return [], Violation.SCOPE_MISSING

    lines = [line for line in raw.strip("\n").split("\n")]
    patterns: list[str] = []
    for line in lines:
        if line.strip() == "":
            continue
        if line != line.strip():
            return [], Violation.SCOPE_MALFORMED
        if line.startswith(("- ", "* ", "+ ")):
            # §9.6 Phase 1 compatibility: a 1.x artifact must be rejected, not
            # silently repaired.
            return [], Violation.SCHEMA_LEGACY_ARTIFACT
        if line == "_No response_":
            return [], Violation.SCOPE_EMPTY
        if any(ch in line for ch in "{}[]!"):
            return [], Violation.SCOPE_MALFORMED
        for segment in line.split("/"):
            if "**" in segment and segment != "**":
                # `**` spans whole segments only; `a**b` has no frozen meaning.
                return [], Violation.SCOPE_MALFORMED
        patterns.append(line)

    if not patterns:
        return [], Violation.SCOPE_EMPTY
    return patterns, None


def _match_segments(pat: list[str], path: list[str]) -> bool:
    """Segment-wise glob match with `**` spanning zero or more segments."""
    if not pat:
        return not path
    head, rest = pat[0], pat[1:]
    if head == "**":
        # zero segments consumed, or one segment consumed and retry
        if _match_segments(rest, path):
            return True
        return bool(path) and _match_segments(pat, path[1:])
    if not path:
        return False
    if not _match_segment(head, path[0]):
        return False
    return _match_segments(rest, path[1:])


def _match_segment(pattern: str, segment: str) -> bool:
    """`*` matches any run of characters except `/`; `?` matches exactly one."""
    regex = ["^"]
    for ch in pattern:
        if ch == "*":
            regex.append("[^/]*")
        elif ch == "?":
            regex.append("[^/]")
        else:
            regex.append(re.escape(ch))
    regex.append("$")
    return re.match("".join(regex), segment) is not None


def match_path(pattern: str, path: str) -> bool:
    """§9.6 semantics. Case-sensitive, repository-relative POSIX paths."""
    return _match_segments(pattern.split("/"), path.split("/"))


def paths_out_of_scope(paths: list[str], patterns: list[str]) -> list[str]:
    """Every changed path must match at least one pattern."""
    return [p for p in paths if not any(match_path(pat, p) for pat in patterns)]


def protected_factory_paths(paths: list[str]) -> list[str]:
    """Concrete changed paths inside the factory control plane."""
    return sorted(p for p in paths
                  if any(match_path(pattern, p)
                         for pattern in PROTECTED_FACTORY_PATTERNS))


def protected_factory_scope(patterns: list[str]) -> list[str]:
    """Scope patterns whose language can reach the protected control plane.

    Directory protection is decided from the first path segment. This catches
    literal scopes, broad scopes such as ``**`` and wildcard aliases such as
    ``f*/**``. Root control files are checked with the frozen path matcher.
    The result is conservative by design: ambiguous breadth fails closed.
    """
    protected = []
    for pattern in patterns:
        first = pattern.split("/", 1)[0]
        directory_reachable = (first == "**" or any(
            _match_segment(first, directory) for directory in PROTECTED_FACTORY_DIRS))
        file_reachable = any(match_path(pattern, path)
                             for path in PROTECTED_FACTORY_FILES)
        if directory_reachable or file_reachable:
            protected.append(pattern)
    return sorted(set(protected))


def is_worker_artifact(pr_body: str) -> bool:
    return bool(WORKER_ARTIFACT_RE.search(pr_body or ""))


def _matching(paths: list[str], patterns) -> list[str]:
    return sorted(p for p in paths if any(match_path(pat, p) for pat in patterns))


def classify_surface(paths: list[str]) -> tuple[str, list[str], list[str]]:
    """Classify a diff against the enforcement surface (#39).

    Returns (verdict, runner_paths, logic_paths) where verdict is one of:

      "clean"   nothing in the enforcement surface — ordinary PR
      "logic"   touches gate logic; advisory, since the trusted main copy judges it
      "runner"  touches the workflow runner; loud, since nothing mechanical can
                protect it and only a human acknowledgement stands behind it

    `runner` outranks `logic`: a PR touching both is reported as `runner`.
    """
    runner = _matching(paths, GATE_RUNNER_PATTERNS)
    logic = _matching(paths, GATE_LOGIC_PATTERNS)
    if runner:
        return "runner", runner, logic
    if logic:
        return "logic", runner, logic
    return "clean", [], []


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


def evaluate(pr_body: str, changed_paths: list[str], story: dict | None,
             tests_passed: bool, story_fetch_error: str | None = None) -> Verdict:
    """Apply every check. Each failure class is independent — one violation
    never masks another, so a red result names all of its causes."""
    verdict = Verdict()

    declared_major = parse_schema_version(pr_body)
    if declared_major is not None and declared_major != SUPPORTED_SCHEMA_MAJOR:
        verdict.fail(
            Violation.SCHEMA_INCOMPATIBLE,
            f"PR declares schema major {declared_major}; this gate implements "
            f"{SUPPORTED_SCHEMA_MAJOR}. §9.1 requires halting, not guessing.",
        )

    if not changed_paths:
        verdict.fail(Violation.NO_CHANGES, "PR has no changed files; nothing to evaluate.")

    if not tests_passed:
        verdict.fail(Violation.TESTS_FAILED, "CI-computed test result was not green.")

    protected = protected_factory_paths(changed_paths)
    if is_worker_artifact(pr_body) and protected:
        verdict.fail(
            Violation.FACTORY_SELF_MODIFICATION_FORBIDDEN,
            "automated worker artifact changes protected factory paths: "
            + ", ".join(protected),
        )

    story_number, link_error = parse_story_link(pr_body)
    if link_error:
        verdict.fail(
            link_error,
            "PR body must contain exactly one canonical `Story: #N` line (§9.5).",
        )
        return verdict

    if story_fetch_error:
        verdict.fail(
            Violation.INPUT_UNAVAILABLE,
            f"could not read story #{story_number}: {story_fetch_error}. Fail-closed (§9.1).",
        )
        return verdict

    if story is None:
        verdict.fail(Violation.STORY_NOT_FOUND, f"story #{story_number} does not exist.")
        return verdict

    labels = {label_name(label) for label in story.get("labels", [])}
    if "type:story" not in labels:
        verdict.fail(
            Violation.STORY_WRONG_TYPE,
            f"issue #{story_number} does not carry `type:story` (§9.5).",
        )
        return verdict

    story_major = parse_schema_version(story.get("body") or "")
    if story_major is not None and story_major != SUPPORTED_SCHEMA_MAJOR:
        verdict.fail(
            Violation.SCHEMA_INCOMPATIBLE,
            f"story #{story_number} declares schema major {story_major}; gate "
            f"implements {SUPPORTED_SCHEMA_MAJOR}.",
        )
        return verdict

    patterns, scope_error = parse_scope(story.get("body") or "")
    if scope_error:
        verdict.fail(
            scope_error,
            f"story #{story_number} `### Scope` is not a valid "
            f"SCHEMA_VERSION {SUPPORTED_SCHEMA_MAJOR}.x scope section (§9.6).",
        )
        return verdict

    out = paths_out_of_scope(changed_paths, patterns)
    if out:
        verdict.fail(
            Violation.OUT_OF_SCOPE,
            "changed paths outside the story's declared scope: "
            + ", ".join(sorted(out))
            + f" (allowed: {', '.join(patterns)})",
        )

    return verdict


def label_name(label) -> str:
    return label.get("name", "") if isinstance(label, dict) else str(label)


# --------------------------------------------------------------------------
# GitHub access — the only I/O in this module
# --------------------------------------------------------------------------


def _api(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "factory-merge-gate",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_pr(repo: str, number: int, token: str) -> tuple[dict, list[str]]:
    pr = _api(f"https://api.github.com/repos/{repo}/pulls/{number}", token)
    paths: list[str] = []
    page = 1
    while True:
        batch = _api(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files"
            f"?per_page=100&page={page}",
            token,
        )
        if not batch:
            break
        for entry in batch:
            paths.append(entry["filename"])
            # A rename is two paths; both must satisfy scope (§9.6).
            if entry.get("previous_filename"):
                paths.append(entry["previous_filename"])
        if len(batch) < 100:
            break
        page += 1
    return pr, paths


def fetch_story(repo: str, number: int, token: str) -> tuple[dict | None, str | None]:
    try:
        return _api(f"https://api.github.com/repos/{repo}/issues/{number}", token), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, None
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, str(exc)


def story_attempt_trace(repo: str, number: int, token: str) -> str:
    timeline = _api(
        f"https://api.github.com/repos/{repo}/issues/{number}/timeline?per_page=100",
        token)
    return obs.story_trace_id(repo, number, timeline)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render_surface(verdict: str, runner: list[str], logic: list[str], context: str) -> str:
    """Human-facing report for `merge-gate-surface`. Advisory in every case —
    this check must never block, or the gate becomes unmodifiable once required
    (#39)."""
    lines = [f"Merge gate — enforcement surface — {context}", ""]
    if verdict == "clean":
        lines.append("SUCCESS — this PR does not touch the gate's enforcement surface.")
    elif verdict == "logic":
        lines.append("NEUTRAL — this PR changes gate logic:")
        lines.append("")
        lines.extend(f"  {p}" for p in logic)
        lines += [
            "",
            "This is allowed and is not a blocking failure. The `merge-gate` verdict",
            "was computed by the trusted copy on `main`, so the proposed change could",
            "not influence its own evaluation. A human `hazard-ack` is expected before",
            "landing (§5.4); its absence is the detectable signal, not its presence.",
        ]
    else:
        lines.append("FAILURE — this PR changes the workflow runner:")
        lines.append("")
        lines.extend(f"  {p}" for p in runner)
        if logic:
            lines.append("")
            lines.append("It also changes gate logic:")
            lines.extend(f"  {p}" for p in logic)
        lines += [
            "",
            "For same-repo `pull_request` events GitHub executes the workflow file",
            "from the PR head, so this change runs its own rewrite and NO check can",
            "protect it — including this one. Human review is the only control here,",
            "and under the shared credential that is a convention, not an enforcement",
            "(§9.7, #39). A `hazard-ack` naming this diff is required before landing.",
        ]
    lines.append("")
    lines.append("Advisory only — this check is not required and must not be made required.")
    return "\n".join(lines)


def render(verdict: Verdict, context: str) -> str:
    lines = [f"Merge gate — {context}", ""]
    if verdict.passed:
        lines.append("PASS — every deterministic check satisfied.")
    else:
        lines.append(f"FAIL — {len(verdict.findings)} violation(s):")
        lines.append("")
        for finding in verdict.findings:
            lines.append(f"  [{finding.code}] {finding.detail}")
    lines.append("")
    lines.append(
        "Inputs used: diff paths, linked story `### Scope`, CI-computed test "
        "result. Labels, Agent-ID, comments, and PR author are deliberately not "
        "consulted (§9.14). This verdict is computed by the trusted copy of the "
        "gate on `main`, never by the copy under review (#39)."
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Deterministic merge gate (§9)")
    parser.add_argument("--pr", type=int, help="pull request number")
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument("--tests-passed", choices=("true", "false"),
                        help="CI-computed test result for this run")
    parser.add_argument("--fixture", help="JSON fixture for offline evaluation")
    parser.add_argument("--mode", choices=("verdict", "surface"), default="verdict",
                        help="verdict: the merge-gate contract check (default). "
                             "surface: the advisory merge-gate-surface classification.")
    args = parser.parse_args(argv)

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as handle:
            data = json.load(handle)
        paths = data.get("changed_paths", [])
        if args.mode == "surface":
            surface, runner, logic = classify_surface(paths)
            print(render_surface(surface, runner, logic, f"fixture {args.fixture}"))
            return 1 if surface == "runner" else 0
        verdict = evaluate(
            pr_body=data.get("pr_body", ""),
            changed_paths=paths,
            story=data.get("story"),
            tests_passed=bool(data.get("tests_passed", True)),
            story_fetch_error=data.get("story_fetch_error"),
        )
        print(render(verdict, f"fixture {args.fixture}"))
        return 0 if verdict.passed else 1

    if not (args.pr and args.repo):
        parser.error("--pr and --repo are required without --fixture")
    if args.mode == "verdict" and not args.tests_passed:
        parser.error("--tests-passed is required in verdict mode")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("Merge gate — FAIL\n\n  [INPUT_UNAVAILABLE] GITHUB_TOKEN is not set; "
              "the gate cannot read the PR and fails closed.")
        return 1

    try:
        pr, changed_paths = fetch_pr(args.repo, args.pr, token)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Merge gate — FAIL\n\n  [INPUT_UNAVAILABLE] could not read PR: {exc}")
        return 1

    if args.mode == "surface":
        surface, runner, logic = classify_surface(changed_paths)
        print(render_surface(surface, runner, logic, f"{args.repo}#{args.pr}"))
        return 1 if surface == "runner" else 0

    story = None
    story_error = None
    story_number, link_error = parse_story_link(pr.get("body") or "")
    if story_number is not None and not link_error:
        story, story_error = fetch_story(args.repo, story_number, token)

    attempt_trace = None
    if story_number is not None:
        try:
            attempt_trace = story_attempt_trace(args.repo, story_number, token)
        except Exception as trace_error:  # noqa: BLE001 - gate verdict stays independent
            obs.operational_log("ERROR", "merge-gate trace could not be read back",
                                exc=trace_error, component="merge-gate",
                                operation="trace", repo=args.repo,
                                story=story_number, pull_request=args.pr)

    verdict = evaluate(
        pr_body=pr.get("body") or "",
        changed_paths=changed_paths,
        story=story,
        tests_passed=(args.tests_passed == "true"),
        story_fetch_error=story_error,
    )
    obs.process_event("merge-gate.evaluated", trace_id=attempt_trace, repo=args.repo,
                      pull_request=args.pr, story=(story or {}).get("number"),
                      passed=verdict.passed,
                      evidence={"violations": [item.code for item in verdict.findings]})
    print(render(verdict, f"{args.repo}#{args.pr}"))
    return 0 if verdict.passed else 1


def guarded_main(argv: list[str]) -> int:
    """Never let an internal error look like a pass, and never let it surface as
    an opaque crash. A gate that cannot evaluate must say so readably and fail."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - deliberately total
        import traceback
        obs.operational_log("CRITICAL", "merge gate failed closed", exc=exc,
                            component="merge-gate", operation="evaluate")

        print("Merge gate — FAIL", file=sys.stdout)
        print("", file=sys.stdout)
        print(f"  [{Violation.INTERNAL_ERROR}] the gate raised "
              f"{type(exc).__name__}: {exc}", file=sys.stdout)
        print("", file=sys.stdout)
        print("  The gate could not complete an evaluation, so it fails closed. "
              "It never passes on an error.", file=sys.stdout)
        print("", file=sys.stdout)
        print("--- traceback (for the fix, not for the verdict) ---", file=sys.stdout)
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main(sys.argv[1:]))
