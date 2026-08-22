# Phase 4 health fixture

Run with an exact deployed commit identity:

```sh
BUILD_SHA=0123456789abcdef0123456789abcdef01234567 python3 app.py --port 8080
curl http://127.0.0.1:8080/health
```

The fixture uses only the Python standard library. See the canonical fixture ADR
for ownership, identity, and non-destructive reset policy.
