# Phase 4 live product module

The isolated live-run module for Story #231. Standard library only.

```sh
BUILD_SHA=0123456789abcdef0123456789abcdef01234567 python3 app.py --port 8080
curl http://127.0.0.1:8080/health
```

`GET /health` returns JSON `{"build_sha":"<sha>"}` with status 200; other paths
return 404. `BUILD_SHA` is the sole authoritative build identity and must be
exactly 40 lowercase hexadecimal characters — missing, abbreviated, uppercase,
or otherwise malformed values fail startup rather than serving invented health
data. See `factory/decisions/phase4-live-fixture.md` for ownership, identity,
and non-destructive reset policy.

Run the tests with `python3 -m unittest discover -s runs/phase4/live_product`.
