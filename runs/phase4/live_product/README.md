# Phase 4 live product module

The isolated module the Phase 4 live run delivers through the factory's own
rails. It is not a retirement-modeling product story and has no dependency on
anything outside this directory — Python standard library only.

```sh
BUILD_SHA=<40 lowercase hex> python3 runs/phase4/live_product/app.py --port 8080
curl -s http://127.0.0.1:8080/health      # {"build_sha":"<the deployed commit>"}
```

```sh
cd runs/phase4/live_product && python3 -m unittest discover -p 'test_*.py'
```

CI runs `factory/gates`, `factory/dispatcher`, `factory/runtime` and
`factory/acceptance` only, so this suite is run from its own directory, as the
modules import their siblings by name.

`BUILD_SHA` is the sole authority for build identity, and it is validated as
exactly 40 lowercase hexadecimal characters. A missing, abbreviated, uppercase,
padded, or otherwise malformed value fails startup — an endpoint that answers
with an invented SHA is worse than one that does not answer, because the live
run's whole claim is that the merged commit is the one serving traffic. The
canonical contract is the fixture ADR
(`factory/decisions/phase4-live-fixture.md`).

## Delivery record

Both attempts landed on one branch and one pull request, per the Story's
acceptance notes and P4-07.

**Attempt 1 — deliberately defective.** `/health` returned
`{"build_sha":"defective"}`: a literal constant, with `BUILD_SHA` neither read
nor validated, and tests written against the constant so they passed anyway.

**Exact-head review findings on Attempt 1.**

- `/health` returns the literal `defective` instead of the injected `BUILD_SHA`,
  so the endpoint reports nothing about the deployed commit and P4-11's
  "returns the authoritative SHA" cannot be satisfied.
- `build_sha()` ignores its `environment` argument and reads no environment at
  all; the fixture ADR requires `BUILD_SHA` to be the sole authoritative source.
- No validation of the 40-lowercase-hex form, so a missing, abbreviated, or
  uppercase value would be served as health data rather than failing startup,
  which the ADR forbids.
- The tests assert the constant, so they would stay green against any invented
  identity; they prove nothing about build identity.

**Attempt 2 — correction on the same PR.** `build_sha()` reads `BUILD_SHA` from
the supplied environment (defaulting to the process environment), requires a
`fullmatch` of exactly 40 lowercase hexadecimal characters, and raises
otherwise, so a bad identity fails before the server binds. `/health` returns
that value as JSON with status 200; every other path is 404. The tests now drive
the injected environment, cover each malformed class named in the finding, and
assert the server refuses to start on the Attempt 1 literal.
