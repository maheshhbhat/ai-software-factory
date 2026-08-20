# Runtime

The persistent local loop that keeps the factory moving: it invokes the merged
dispatcher on an interval and turns each canonical `DISPATCH` line into a worker
wake-up.

**The relay it deletes.** Before this, a human read a newly-authorized issue
number and typed `work #N`. The labels had already made the decision, so that
touch carried no judgment — a *relay* touch, the one class
`architecture-v2.1.md` §7 requires to trend to zero.

## The design rule that keeps it small

**The runtime holds no judgment.** It does not decide what is authorized,
eligible, in scope, or within WIP; it does not recover leases; it never edits a
story's lifecycle. All of that belongs to `factory/dispatcher/dispatcher.py`,
which it runs as a subprocess. Everything protecting the repository from a bad
dispatch — the authorization chain, the trust boundary, WIP and attempt limits,
the required merge gate — sits upstream. **If this ever grows a policy decision,
that is the bug.**

Per poll: run the dispatcher with claiming enabled → read only canonical
`DISPATCH story=#N project=#P agent=<id>` lines → launch the worker once each.

## Idempotency comes from GitHub

A claimed story is no longer `story:ready`, so the next poll's dispatcher does
not offer it. The in-process `seen` set only guards against double-launching
inside one run; nothing persists it, and a restart with empty local state
re-derives identical behaviour. No local cursor is authoritative — losing it
costs latency, never correctness.

## Fail closed, visibly

| Situation | Behaviour |
|---|---|
| No `GITHUB_TOKEN` / `GH_TOKEN` | refuse to poll at all |
| Dispatcher exits non-zero | report; **never** read as "no work to do" |
| A `DISPATCH`-shaped line that is not canonical | abort the poll, launch nothing |
| Worker fails to launch | loud — the story is claimed in GitHub with nothing working it |

The parser is strict for a reason: that one line is the boundary between
deciding and doing, so a near-miss must not be read as a dispatch. Reordered
fields, a missing `#`, an uppercase agent, or a trailing `; rm -rf /` are all
errors, not shrugs.

The runtime never edits lifecycle to compensate for its own failure. A claimed
story with a dead worker is recovered by the dispatcher's §9.4 lease expiry,
which is the component that owns that decision.

## Worker adapter

`FACTORY_WORKER_CMD` is the whole extension point — point it at Codex or any
other launcher and dispatcher semantics do not change. Placeholders `{story}`,
`{project}`, `{agent}` are substituted by name.

With it unset, the default adapter announces on stdout:

```
WAKE worker=claude-delivery story=#64 project=#55
```

Under this repository's existing monitor, one stdout line is one notification to
the delivery worker — which is the wake-up. No parallel orchestration framework
is introduced.

Note what an adapter never receives: no spec, no scope, no acceptance criteria.
The worker reconstructs context from GitHub itself (`architecture-v2.1.md` §4 —
the moment a queue item copies business context, the relay has been rebuilt as
infrastructure).

## Running it

```sh
export GITHUB_TOKEN=$(gh auth token)

# watch continuously (this is the service)
python3 factory/runtime/poller.py --repo owner/name --commitment 54 --interval 60

# one cycle and exit
python3 factory/runtime/poller.py --repo owner/name --commitment 54 --once

# observe decisions without claiming anything
python3 factory/runtime/poller.py --repo owner/name --commitment 54 --once --dry-run
```

Stop it with `ctrl-c`, or by stopping the monitor task running it. Status is the
stdout stream: every poll that dispatches prints a `[poller]` line, and every
failure prints one too.

## Tests

```sh
cd factory/runtime && python3 -m unittest discover -p 'test_*.py' -v
```

Standard library only. The parsing tests carry the weight — the near-miss cases
matter more than the happy path.
