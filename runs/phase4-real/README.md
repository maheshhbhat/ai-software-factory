# Phase 4 real-delivery verification runs

Each directory here is one operator-invoked run of
`factory/acceptance/phase4_real.py`: the production `./poll.sh` service, the
production worker and reviewer, the real engine, the real merge gate — nothing
substituted. For the criteria its `evidence.json` names, a run here supersedes
the `runs/phase4/` ledger, whose evidence came from a harness that replaced
the engine, the launch path, and the environment (`phase4_live.py`).

Old runs stay as audit evidence per the phase4-live-fixture ADR. The evidence
counter (`requirement_coverage.py`) still reads `runs/phase4/`; teaching it to
prefer these runs belongs to Project #327 and is deliberately not done here.
