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
| `SELF_MODIFICATION` | §9.14 | the PR edits the gate's own enforcement surface |
| `TESTS_FAILED` | §9.14 | the CI-computed test result was not green |
| `NO_CHANGES` | — | nothing to evaluate |
| `INPUT_UNAVAILABLE` | §9.1 | an input could not be read — fail closed |

Each class fails independently, so a red result names every cause rather than
stopping at the first.

## What it deliberately does not read

Labels, `Agent-ID`, comments, review state, and PR author identity. All are
forgeable by anything holding the repository credential (§9.7), so none may
decide a verdict. `TestTrustBoundary` in the test suite asserts this: adding
labels or approval-sounding claims to a PR body does not change the outcome.

The four inputs it does trust are the diff, the linked story's `### Scope`, the
test result computed inside the workflow run, and the workflow boundary itself.

## The bootstrap property

The gate fails closed on any PR that modifies `.github/workflows/merge-gate.yml`
or `factory/gates/**` — including the PR that introduces it. A gate cannot
vouch for a change to itself; that is what makes the workflow boundary a usable
trust anchor rather than a circular one. Such PRs need human review, which is
correct: they are hazard-path changes in the sense of `architecture-v2.1.md` §5.

## Running it

```sh
# offline, against a JSON fixture — no network, no token
python3 factory/gates/merge_gate.py --fixture path/to/fixture.json

# against a live PR
GITHUB_TOKEN=... python3 factory/gates/merge_gate.py \
    --repo owner/name --pr 123 --tests-passed true
```

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
