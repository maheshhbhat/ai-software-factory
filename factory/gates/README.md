# Merge gate

Deterministic CI check for pull requests targeting `main`. Implements
`factory/spec/state-schema.md` §9; that document is authoritative and this one
only describes the implementation.

**Status: not a required check.** Per §9.13 it is proven green/red first; making
it required — and simultaneously setting required approvals to 0 — is a single
atomic ruleset edit under separate authorization.

## What it checks

| Violation code | Contract | Meaning |
|---|---|---|
| `LINK_MISSING` / `LINK_DUPLICATE` | §9.5 | PR body must carry exactly one `Story: #N` line |
| `STORY_NOT_FOUND` / `STORY_WRONG_TYPE` | §9.5 | the referenced issue must exist and carry `type:story` |
| `SCHEMA_INCOMPATIBLE` | §9.1 | a declared schema major this gate does not implement — halt, never guess |
| `SCHEMA_LEGACY_ARTIFACT` | §9.6 | a `1.x` story (bulleted `### Scope`) — reject, never silently repair |
| `SCOPE_MISSING` / `SCOPE_MALFORMED` / `SCOPE_EMPTY` | §9.6 | the scope section is absent, unparseable, or matches nothing |
| `OUT_OF_SCOPE` | §9.6 | a changed path matches no declared pattern |
| `TESTS_FAILED` | §9.14 | the CI-computed test result was not green |
| `NO_CHANGES` | — | nothing to evaluate |
| `INPUT_UNAVAILABLE` | §9.1 | an input could not be read — fail closed |
| `INTERNAL_ERROR` | §9.14 | the gate itself raised — reported readably, never a pass |

Each class fails independently, so a red result names every cause rather than
stopping at the first.

## What it deliberately does not read

Labels, `Agent-ID`, comments, review state, and PR author identity. All are
forgeable by anything holding the repository credential (§9.7), so none may
decide a verdict. `TestTrustBoundary` in the test suite asserts this: adding
labels or approval-sounding claims to a PR body does not change the outcome.

The four inputs it does trust are the diff, the linked story's `### Scope`, the
test result computed inside the workflow run, and the workflow boundary itself.

## Two checks, and why the second must never be required

| Check | Required? | What it says |
|---|---|---|
| `merge-gate` | intended (§9.13) | the contract verdict, computed by the **trusted `main` copy** of this file |
| `merge-gate-surface` | **never** | advisory classification: success / success+warning / **failure** for a runner change |

**The trusted-main rule.** The workflow checks out `main` into `trusted/` and runs
*that* copy against the PR's data. The code deciding a verdict is therefore always
a version that already landed, so a pull request cannot weaken the rules it is
judged by; a proposed change takes effect only after it merges. This replaces the
older `SELF_MODIFICATION` refusal with a structural guarantee, and it is what
allows gate-logic PRs to pass on their merits.

**Why `merge-gate-surface` stays advisory.** Once `merge-gate` is required with an
empty bypass list, a *second* required check that fails on gate changes would make
the gate permanently unmodifiable — no human action could turn it green, so no bug
fix could ever land. The surface check therefore reports and annotates, and never
blocks.

**The half that cannot be protected.** For same-repo `pull_request` events GitHub
executes the workflow file **from the PR head**. A PR that rewrites
`.github/workflows/merge-gate.yml` runs its own rewrite, so no check can protect
the file that defines the check — including this one. That path is covered by human
review, a `hazard-ack` naming the diff, and the audit trail. Under a shared
credential that is a convention rather than an enforcement: a *missing* ack on a
landed runner change is unforgeable evidence nobody reviewed it, while a *present*
ack proves only that the text exists.

## A note on the runner shell

The workflow's evaluate step sets `shell: bash` and `set -o pipefail`
explicitly. GitHub's default `run:` shell on Linux is `bash -e` **without**
pipefail, so piping the gate into `tee` reports `tee`'s exit status and the
check goes green while the gate is failing. That happened on the gate's own
first CI run and was caught by comparing the reported check result against the
gate's printed verdict. A control that cannot fail is worse than no control,
so this is not a stylistic preference.

## Running it

```sh
# offline, against a JSON fixture — no network, no token
python3 factory/gates/merge_gate.py --fixture path/to/fixture.json

# against a live PR
GITHUB_TOKEN=... python3 factory/gates/merge_gate.py \
    --repo owner/name --pr 123 --tests-passed true

# the advisory enforcement-surface classification
GITHUB_TOKEN=... python3 factory/gates/merge_gate.py \
    --mode surface --repo owner/name --pr 123
```

Exit status in `--mode surface` is 0 for an ordinary or gate-logic PR and 1 for a
runner change; the workflow turns that into an annotation, never a blocking
failure.

Exit status is 0 on pass, 1 on any violation.

Fixture shape:

```json
{
  "pr_body": "Story: #42\n",
  "changed_paths": ["src/a.py"],
  "story": {"body": "### Scope\n\nsrc/**\n", "labels": [{"name": "type:story"}]},
  "tests_passed": true
}
```

## Tests

```sh
cd factory/gates && python3 -m unittest discover -p 'test_*.py' -v
```

Standard library only — no dependencies to install, and nothing to keep in sync
with a lockfile. Coverage includes the `*` / `**` glob edge cases frozen in
§9.6, fail-closed behaviour when GitHub data is unavailable, and the assertion
that forgeable inputs cannot change a verdict.
