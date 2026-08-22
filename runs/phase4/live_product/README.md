# Phase 4 live product module

The isolated module the Phase 4 live run deploys and executes. Standard library
only, no external data dependency, no product data. It is not part of any
retirement-modeling product story.

Run it with an exact deployed commit identity:

```sh
BUILD_SHA=0123456789abcdef0123456789abcdef01234567 python3 app.py --port 8080
curl http://127.0.0.1:8080/health
# {"build_sha":"0123456789abcdef0123456789abcdef01234567"}
```

Tests:

```sh
python3 -m unittest discover -s runs/phase4/live_product -p 'test_*.py'
```

`GET /health` returns `200` with JSON `{"build_sha":"<sha>"}`; every other path
returns `404`. The sole authoritative build identity is `BUILD_SHA`, injected by
the deployment from the exact deployed Git commit, and it must be exactly 40
lowercase hexadecimal characters — missing, abbreviated, uppercase, or otherwise
malformed values raise at startup instead of serving invented health data.
Ownership, interface, and non-destructive reset policy are fixed by
`factory/decisions/phase4-live-fixture.md`; the canonical baseline the module
mirrors is `factory/fixtures/phase4_health/`.

These tests live outside the four suites the merge gate runs
(`factory/gates`, `factory/dispatcher`, `factory/runtime`, `factory/acceptance`)
and outside `factory/coverage_report.py`'s declared suites, so they contribute no
CI verdict and no coverage percentage. The hermetic proof of this interface is
`factory/acceptance/test_phase4_fixture.py`; the live proof is Story #220's
`runs/phase4/evidence.json`.

`FINDINGS.md` records the Attempt 1 review outcome this module was corrected
against.
