# Runtime

The persistent local loop that keeps the factory moving: it invokes the merged
dispatcher on an interval and turns each canonical `DISPATCH` line into a worker
wake-up.

**The relay it deletes.** Before this, a human read a newly-authorized issue
number and typed `work #N`. The labels had already made the decision, so that
touch carried no judgment — a *relay* touch, the one class
`architecture-v2.1.md` §7 requires to trend to zero.

## Two passes per cycle

**1 · Continuation** (`continuation.py`) — consume human decision comments on
projects sitting at `project:awaiting-ready` or `project:awaiting-acceptance`.
This exists because a bell is rung as a *comment on an existing issue*, which
new-issue discovery cannot see. Observed live three times (#55, #61, #66): a
valid approval sat in GitHub while the lifecycle waited for someone to type
`work #N`.

**2 · Dispatch** (`poller.py` + the dispatcher) — claim and wake, as below.

Continuation runs first, so an approval and the work it unblocks can land in the
same cycle. Its failures are isolated: a malformed decision comment reports and
is skipped, and dispatch still runs. Coupling them would let one bad comment
halt the whole factory.

### What continuation can and cannot prove

Under the single-credential threat model (§9.7) the agent writes through the
CTO's account, so **every comment this factory posts is `OWNER`-authored too**.
Author association cannot distinguish a human decision from an agent-written
one. What separates them is a **heading convention** — a decision carries the
exact §5 heading (`## Plan approval`, `## Acceptance`) while recording comments
use different ones (`## Approval recorded`, `## Approval superseded`) — and a
test pins the real recording format so a future edit cannot quietly turn it into
a self-approval.

The one check that holds regardless of who typed what is the **§5.1 binding
rule**: an approval is valid only while the project's criteria section still
matches what the approval quoted. Criteria edited after approval fail closed.
Ticking a checkbox does not count as an amendment; changing the wording does.

Everything ambiguous — conflicting owner decisions, an unreadable decision line,
an unrecognized verdict — fails closed with a named reason and no transition.

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

## Worker contract (#84)

Workers are execution engines behind one factory-owned contract
(`workers.py`). **The factory owns policy; workers execute.** No adapter holds
authorization, lifecycle, WIP, retry, or terminal-state logic — a test asserts
that by inspecting the module, because a policy check smuggled into an adapter
is invisible in behavioural tests until it disagrees with GitHub.

A worker declares four things: a name (a valid `Agent-ID`), a launch command, an
optional health probe, and its capabilities. Configuration only:

```sh
export FACTORY_WORKER_ORDER=claude-delivery,codex-delivery

export FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH='/path/wake-claude {story}'
export FACTORY_WORKER_CLAUDE_DELIVERY_HEALTH='/path/claude-probe'
export FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES=delivery

export FACTORY_WORKER_CODEX_DELIVERY_LAUNCH='/path/wake-codex {story}'
export FACTORY_WORKER_CODEX_DELIVERY_HEALTH='/path/codex-probe'
export FACTORY_WORKER_CODEX_DELIVERY_CAPABILITIES=delivery
```

Swapping the preferred engine is a change to `FACTORY_WORKER_ORDER` and nothing
else. With no workers declared, the legacy single-command path
(`FACTORY_WORKER_CMD`) is used unchanged.

### Selection is configuration, not judgment

Capability is declared, not inferred; health is probed; the configured order
decides preference. Same configuration and same probe results give the same
answer every time — no scoring, no tie-breaking, no model judgment.

Health and capability are **routing** observations. They decide which engine
gets a Story. They never decide whether a Story is authorized, how much WIP is
in use, how much retry budget remains, or whether anything is terminal — those
live in GitHub (§9.12), and a local probe must not be able to contradict them.

### The property that makes failover safe

Failover is the first mechanism in the factory that could put **two workers on
one Story**. Two rules prevent it:

1. **Launch sequentially, stop at the first success** — at most one launch can
   succeed, so at most one worker starts.
2. **A definite failure is not an ambiguous outcome.** `command not found` or a
   non-zero exit *proves* the worker did not start, so falling back is safe. A
   timeout proves nothing — the worker may be running right now — so it fails
   closed with **no fallback**, and the bounded §9.4 lease recovers the claim.

Rule 2 is the one to defend in review. Treating "I don't know" as "it failed" is
exactly how a system ends up with two workers writing to one branch. A Story
claimed with no worker running is a bounded, recoverable inconvenience; two
workers on one Story is a corruption.

## The launch bridge (#90)

`bridge.py` is the factory's own handoff to a real CLI engine — the thing a
worker declaration points at:

```sh
export FACTORY_WORKER_CODEX_DELIVERY_LAUNCH='python3 factory/runtime/bridge.py \
    --engine codex --story {story} --project {project}'
```

Before it existed, the runtime printed a `WAKE` line and a human-configured
standing CLI session picked it up. That session was not a factory-owned
launcher, so swappability was proven in tests and not in the world.

The bridge is an *implementation*, not another orchestrator: `workers.py` still
chooses the engine and enforces the failover rules. The bridge only turns
`(engine, story, project)` into a CLI invocation, and prints the exact command
so the handoff is observable.

**What the engine is told:** repository, story number, project number. Nothing
else — no spec, no scope, no criteria. It reads the substrate itself (§4).

**Why the prompt is narrow:** an engine invoked here runs unattended. The prompt
states one bounded task and says to do nothing else. Widening it is not a
convenience, it is handing an autonomous agent an open mandate with no human in
the loop. The bounds protecting the repository are upstream — authorization
chain, WIP, merge gate; the bounds protecting *this invocation* are the prompt
and the timeout.

A bridge timeout exits non-zero and says the engine may still be running;
`workers.py` maps its own timeout to `AMBIGUOUS` and refuses to fall back.

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
