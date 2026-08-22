# Phase 4 deterministic coverage baseline

Measured 2026-08-22 with:

```sh
python3 factory/coverage_report.py --python /tmp/factory-cov/bin/python --check
```

Both isolated measurements were identical and every contributing suite passed.
This is the deterministic pre-live baseline; Story #220 must rerun it and report
the live `--with-e2e` result separately.

| Layer | Alone | Unique contribution |
|---|---:|---:|
| Unit | 69.4% | +15.1 points |
| Integration | 56.1% | +1.3 points |
| Acceptance | 61.3% | +6.6 points |
| Combined | 80.6% | — |

| Phase 4 module | Combined coverage |
|---|---:|
| Worker wrapper | 72.3% |
| Review wrapper | 64.4% |
| Sampling | 85.0% |
| Review routing | 94.1% |
| Review link | 92.5% |
| Poller | 74.5% |

Named uncovered risks:

- Worker and reviewer share one GitHub principal until Project #221.
- Live GitHub, engine, merge, deployment, and sampling wiring remains unproven
  until Story #220 produces `runs/phase4/evidence.json`.
- Line coverage cannot prove semantic correctness or independent authorization.

No threshold is proposed or enforced. The Phase 4 requirement checker confirms
all P4-01 through P4-16 criteria have named hermetic evidence and deliberately
fails today on the ten wiring criteria awaiting Story #220 live evidence.
