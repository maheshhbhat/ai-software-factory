# Runtime

The persistent local loop that keeps the factory moving: it invokes the merged
dispatcher on an interval and turns each canonical `DISPATCH` line into a worker
wake-up.

## Human plan-approval helper

From the repository root, `./approve-plan.sh PROJECT_NUMBER` fetches the live
project criteria, previews the exact §5.1 approval comment, and posts it only
after the operator types `approved`. It removes error-prone copying but does not
make or infer the decision. An absent/malformed criteria section, a failed
GitHub read, or any other confirmation exits nonzero and posts nothing.

**The relay it deletes.** Before this, a human read a newly-authorized issue
number and typed `work #N`. The labels had already made the decision, so that
touch carried no judgment — a *relay* touch, the one class
`architecture-v2.1.md` §7 requires to trend to zero.

## Six passes per cycle

**1 · Review link** (`review_link.py`) — turn a delivery pull request into the
lifecycle transitions it implies: `story:claimed → story:in-review` when a
§9.5-linked PR is open, and `story:in-review → story:merged` (closed as
completed) when it merges. See *The review link* below.

**2 · Continuation** (`continuation.py`) — consume human decision comments on
projects sitting at `project:awaiting-ready` or `project:awaiting-acceptance`.
This exists because a bell is rung as a *comment on an existing issue*, which
new-issue discovery cannot see. Observed live three times (#55, #61, #66): a
valid approval sat in GitHub while the lifecycle waited for someone to type
`work #N`.

**3 · Sequencer** (`sequencer.py`) — advance dependency-satisfied blocked
stories to ready, and fully delivered active projects to awaiting acceptance.
Both decisions are derived from current GitHub state and are idempotent.

**4 · Human queue** (`humanqueue.py`) — say what is waiting on a person. See
*The human queue* below.

**5 · Dispatch** (`poller.py` + the dispatcher) — claim and wake, as below.

**6 · Completion** (`completion.py`) — when a worker finishes successfully, ask
whether its Story is done. See *The completion path* below.

The order is load-bearing at both ends. Review link runs **first** so a story
whose delivery merged leaves `story:claimed` before WIP is counted — otherwise
finished work keeps a worker slot it no longer needs. Continuation and
sequencing run before dispatch so an approval and the work it unblocks land in
the same cycle. The human queue runs after both, so its list describes this cycle rather than the
last one.

Every pass is isolated: a failure reports and the poll continues. Coupling them
would let one malformed comment, or one ambiguous pull request, halt the whole
factory. Completion is isolated for the same reason — if it fails the Story
stays claimed and the §9.4 lease resolves it.

## The review link (#111)

`story:claimed → story:in-review` and `story:in-review → story:merged` are
transitions §4.2 has always specified and nothing ever performed. A human moved
both labels on every story the factory delivered, which is the relay Phase 2
exists to delete — and it was not hypothetical: **#97 sat open at
`story:in-review` for four days with its delivery PR #98 merged**, because the
second route had no implementation to run.

§9.11 listed the merge route as documentation-only, "gated on §9.13". §9.13 is
complete — `merge-gate` is required, approvals are at zero — so the gate that
deferred it has been satisfied.

**Why these two may be mechanical.** The only thing this reads is whether a pull
request carrying the canonical `Story: #N` line is open or merged, and GitHub's
merge state is not something a worker asserts: it is the outcome of the required
gate the worker's own credential cannot influence (§9.14). That is the whole
difference from `story:completed`, which needs §9.16's much narrower proof
because nothing outside the factory had ruled on the work.

**One writer per transition.** A `story:claimed` story whose linked PR has
*already* merged is deliberately left alone here — the dispatcher's §9.4
recovery pass already reconciles exactly that case, and two components writing
one transition is how a lifecycle stops being auditable. This pass names that
owner and stands down.

Ambiguity writes nothing and says why: two linked PRs, a duplicate `Story:`
line, or a PR closed without merging. The last is not an error the factory can
resolve — work was delivered and then rejected, and what happens next is a
human's decision.

## The human queue (#111)

§9.11 ends with a rule the factory did not keep: *"A transition with no live
route is logged and surfaced, never discarded — an unrouted transition is a
contract gap, and silence would hide it."*

The silence was measurable. `continuation.run` skips a project whose outcome is
`NO_DECISION` without printing anything, so a project at
`project:awaiting-ready` with no approval produced **no output at all**, on
every poll, indefinitely — which is how #55, #61 and #66 each sat waiting until
somebody happened to look. A story at `story:blocked:scope` appeared only as one
skip line among the ineligible, and a poisoned story appeared nowhere at all,
because until #110 poisoning closed the issue.

The pass is deliberately the dumbest thing that works: **enumerate everything
waiting, every poll, from durable state.** One canonical line per artifact plus
one structured runlog event, each carrying identity, state, link and the action
required.

```
[human-queue] 2 artifact(s) waiting on a human
HUMAN-QUEUE artifact=#109 state=project:awaiting-ready url=… action='approve the …'
HUMAN-QUEUE artifact=#42 state=story:blocked:poison url=… action='rescue per §4.3.6 …'
```

**Each action names the exact recordable form** (#122), not merely the decision.
The queue used to say "post a `## Plan approval` comment" and stop. On #109 the
CTO did exactly that, wrote `APPROVED.`, and the continuation pass refused it —
§5.1 declares a machine-readable shape carrying a literal `decision:` line, and
the queue had not said so. The parser was right; reading prose to decide whether
the factory is authorized is what §9.9 forbids. The defect was in the
instruction, and it cost a relay touch — the one metric this phase is measured
on driving to zero.

`HUMAN_QUEUE_FORMATS` pins each promised literal against the regex that will
actually read the reply, because the failure mode is **drift**: an instruction
and a parser that quietly stop agreeing. A test asserting a fixed expected string
would pass forever while the parser moved underneath it. Two of the four states
declare `None` — no automated pass consumes a poison rescue or a scope
resolution — and the action says so, because an invented format implies a
machine is waiting when none is.

**It holds no state, and that is the design.** Nothing records that an artifact
was announced, so an artifact still waiting is announced again and one that
stopped waiting stops being announced — with no cursor to go stale and nothing
to reconcile after a restart. A notifier that remembers what it has already said
goes quiet about a problem precisely because the problem is old. "Already
notified" is not a state this pass can be in.

The line names the artifact rather than describing it, like the dispatcher's
`DISPATCH` line and for the same reason (`architecture-v2.1.md` §4). The
transport is the monitor that already turns one stdout line into one
notification — no webhook, no secret, no outbound dependency.

**The trust boundary runs the other way here.** §9.9 says untrusted text may be
read "for context, for a human's queue". A queue entry authorizes nothing, so an
untrusted artifact is listed and *marked* rather than hidden; hiding it would be
the worse error. It never mutates a lifecycle: an artifact waiting on a human is
waiting because no component may decide it, and a notifier that could change
what it reports on would not be a notifier.

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

**`poller.py` holds no judgment.** It does not decide what is authorized,
eligible, in scope, or within WIP; it does not recover leases; and it decides
nothing about a story's lifecycle. All of that belongs to
`factory/dispatcher/dispatcher.py`, which it runs as a subprocess, or to the
narrow modules beside it — `continuation.py` for a project at a bell,
`completion.py` for a Story a worker has finished. Everything protecting the
repository from a bad dispatch — the authorization chain, the trust boundary,
WIP and attempt limits, the required merge gate — sits upstream. **If the poll
loop ever grows a policy decision, that is the bug**, and a test asserts it by
inspection.

Per poll: run the dispatcher with claiming enabled → read only canonical
`DISPATCH story=#N project=#P agent=<id>` lines → launch the worker once each →
ask the completion path whether each finished worker ends its Story.

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
export FACTORY_WORKER_ORDER=capacity-delivery
export FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH='/path/capacity-worker {story}'
export FACTORY_WORKER_CAPACITY_DELIVERY_CAPABILITIES=delivery
```

Provider and model preference belongs to Capacity Pool, not worker order. With
no workers declared, the legacy single-command path
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
export FACTORY_WORKER_CAPACITY_DELIVERY_LAUNCH='python3 factory/runtime/bridge.py \
    --story {story} --project {project}'
```

Before it existed, the runtime printed a `WAKE` line and a human-configured
standing CLI session picked it up. That session was not a factory-owned
launcher, so swappability was proven in tests and not in the world.

The bridge is an *implementation*, not another orchestrator. It supplies the
bounded acknowledgement prompt and verifies the durable comment. Capacity Pool
alone chooses and invokes the provider/model, applies fallback policy, and owns
the combined envelope.

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

### Proof of action, not proof of exit (#97)

An exit code proves the process ended. It does not prove the assignment was
carried out — and the difference is not academic. The first unattended Claude
run (#96) ended cleanly having posted nothing, because `--permission-mode
acceptEdits` authorises file edits the prompt forbids while leaving Bash, where
the whole assignment lives, behind an approval prompt no unattended run can
answer. The runtime recorded `LAUNCHED`, which is a *definite success*, so
failover was correctly suppressed and the silent no-op became terminal.

Two changes close that:

- The Claude engine is granted exactly the commands the prompt names
  (`--allowedTools`), not a blanket permission mode. A command that is not
  listed is a command it cannot run.
- The bridge asks GitHub whether an acknowledgement appeared **after this
  invocation's launch instant**, and that instant is GitHub's own clock, not the
  local one. No acknowledgement means the engine did not do the work, and the
  bridge exits non-zero — a definite failure, which is the one verdict that lets
  another engine try.

Four outcomes, four meanings, and they must stay distinct:

| Outcome | Verdict | Why |
|---|---|---|
| Acknowledgement in the window | success | the work is durable in GitHub |
| Clean exit, no acknowledgement | definite failure | proven not done; failover is safe |
| Non-zero exit, acknowledgement present | success | the work was done; the crash came after |
| Launch cap reached | ambiguous | the engine may be mid-task; never fall back |

The ambiguous row belongs to the *parent*, not to this process. `workers.launch`
maps every non-zero exit to `FAILED`, so a bridge that timed out first would
report "it did not start" about an engine that is running right now, and a second
engine would be launched onto the Story. `FACTORY_BRIDGE_TIMEOUT` is therefore
clamped to stay above `workers.LAUNCH_TIMEOUT_SECONDS`: the timeout that must win
is the one that maps to `AMBIGUOUS`.

The third row is the thesis applied in the other direction. An engine that posts
and then dies in teardown *did the work*, and calling that a definite failure
sends a second engine to post a second acknowledgement. Only evidence overrides
the engine's own verdict there — an unverifiable answer leaves the non-zero exit
standing.

### Three things the check must not confuse

**Ignorance is not evidence.** A read that fails is retried; only when every
attempt fails is the answer unknowable, and an unknowable answer reports the
engine's own exit status — the pre-#97 behaviour, kept as a stated limitation.
Spending ignorance as evidence produces a failure verdict, and a failure verdict
is what puts a second worker on a Story. The poller refuses to run without a
token, so the blind path is not the normal one.

**The clock belongs to the server.** Comparing GitHub's timestamps against a
local clock makes the verdict depend on machine skew, and a fast local clock
hides the acknowledgement — which reads as a definite failure and invites the
duplicate.

**The window is the invocation, not the issue.** Asking only for comments since
the launch instant is what keeps the check cheap and correct. Reading the whole
comment list would be paginated: past 100 comments the new acknowledgement lands
on page two, unseen, and every worker that *did* post would be called a failure.

**An early `no` is not evidence.** The first read firing before GitHub has made
the comment visible is the entire reason the check retries, so the *last
authoritative* answer decides. If the tail of the loop goes blind, the waiting
never happened and the answer is ignorance — not the failure verdict that would
put a second engine on the Story. Both exit paths consult the same patient
verdict; giving the failure path its own impatient read is how a posted
acknowledgement gets called a no-op.

**The heading is generated text.** The engine is asked to produce
`## Worker acknowledgement`, and an exact prefix match on LLM output fails in the
dangerous direction — `**## Worker acknowledgement**` or a line of preface would
make a real acknowledgement invisible. Matching discounts emphasis and heading
level across the first few lines, while staying anchored to the start of a line
so that prose *about* an acknowledgement is not mistaken for one.

### Two limits worth naming

**The check spends the launch budget.** All of it runs inside
`workers.LAUNCH_TIMEOUT_SECONDS`, which kills this process. If the check could
outlast that, a slow engine would be killed mid-check and reported `AMBIGUOUS` —
suppressing failover and making the `FAILED` verdict unreachable exactly when it
matters. The check's budget is therefore *derived* from that cap — a third of it,
with the read timeout computed from the remainder — rather than hand-tuned to sit
under it. Change the cap and the check follows; tests pin the derivation, not the
numbers.

**Attribution inside the window is by time, not by author.** Under the
single-credential model every comment has the same author, so an acknowledgement
that lands during this invocation's window is credited to this invocation. A
straggler from an earlier engine — killed at the launch cap, still alive, posting
late — can therefore be credited to its successor. The consequence is a
misattributed audit line, not a duplicate or a lost Story, and it is recorded
here rather than papered over.

## The completion path (#104)

The clean-room verification under Project #95 found the lifecycle stopping one
step short. A Story moved `story:ready → story:claimed`, the worker ran, the
bridge verified a durable acknowledgement — and the Story then sat at
`story:claimed`, waiting for a human to finish a transition the evidence had
already decided.

**"Sat there" is not the same as "stayed still."** `story:claimed` is a lease.
After `CLAIM_LEASE` the dispatcher finds a claim with no linked pull request,
cannot tell a finished worker from a dead one, and recovers the Story to
`story:ready` — so the next poll dispatches it again and a second
acknowledgement appears. Project #95's *exactly one acknowledgement* criterion
could not hold for longer than one lease period.

### Why a component can decide this now

§9.4.1 says why the dispatcher does not try: *durable evidence cannot
distinguish a worker that died before starting from one that ran correctly and
produced no pull request.* That was true when it was written. #98 made the
missing evidence exist — the bounded assignment ends in an acknowledgement
comment, durable in GitHub, verified before the launch is reported as success.

### The preconditions, all of them

| # | Condition | Read from |
|---|---|---|
| 1 | the launch reported `LAUNCHED` — a *definite* success | `workers.py` |
| 2 | the Story is still `story:claimed` | the issue, re-read |
| 3 | **no pull request links to the Story** (§9.5) | open + closed PRs |
| 4 | a `story:claimed` `labeled` event exists | the timeline |
| 5 | an acknowledgement was posted at or after that instant | the comments |

Anything else leaves the Story exactly as it is, with a named reason. The
asymmetry is deliberate: a wrong "no" costs one lease period and a §9.4
recovery, while a wrong "yes" closes a Story whose work never happened — and
§9.3 says no component reopens a closed issue.

Condition 3 is what keeps this safe as worker assignments grow. A worker that
produced a pull request has a deliverable and belongs to review and the merge
gate; this path only ever completes work that finished with **nothing to
merge**. A test pins the coupling from the other side too: the bridge's prompt
must still forbid files, branches and pull requests, so widening the assignment
fails the build instead of quietly cancelling Stories that were meant to deliver
code.

### Which state

`story:completed` (§9.16) — *the bounded assignment succeeded and required no
deliverable*. A terminal **success**, closed as completed. `Attempt` is
untouched: the attempt was dispatched and it succeeded.

Not `story:merged`, because nothing merged and nothing was going to. Not
`story:cancelled`, because nothing was called off — cancellation means work was
deliberately stopped, and a factory that files its own successes there cannot
afterwards tell anyone, including itself, which of its stories worked. The
schema now carries two distinct terminal successes for the two distinct ways
work can be done: through the merge path, or with nothing to merge.

The label is defined in `dispatcher.py` alongside the rest of the lifecycle
vocabulary. This module chooses the transition; it does not get to invent the
state it transitions to.

### Where the judgment lives

`poller.py` calls `completion.record_success` and prints what comes back. Every
precondition, the §9.5 read, the target state and the recorded reason are in
`completion.py`, and the primitives are *imported from the dispatcher* rather
than restated — one definition of "a pull request links to this Story", one
definition of what an acknowledgement looks like. Two definitions drifting apart
is how a Story with a deliverable gets closed as having none.

The write follows §9.2: re-read, verify the `from` state, then one PATCH with
the complete final label set and the closure. The reason comment is posted
*first*, so a crash between the two writes leaves a claimed Story carrying an
explanation — visible and lease-recoverable — rather than a terminal Story with
no recorded reason.

### One limit worth naming

The completion pass is triggered by a launch *returning* success, so it fits an
engine the factory invoked and waited for.
Under the legacy `FACTORY_WORKER_CMD` adapter — a `WAKE` line for a standing
session to pick up later — the launch returns before the worker has done
anything, so nothing is proved, nothing is completed, and §9.4 remains the
resolution exactly as before.

## Operational logging (#104)

### Live observability

Each run writes three independent files under `FACTORY_RUN_DIR` (default
`factory/runtime/logs/current`): `process-events.jsonl` for lifecycle facts,
`operations.jsonl` for severity-classified diagnostics and full tracebacks, and
`telemetry.jsonl` for heartbeats, timings, and engine usage. These streams are
not interchangeable and their writers reject fields from another stream.

Every long-running component emits a supervisor-owned heartbeat every five
seconds. A component is shown as `STUCK` when no heartbeat arrives for fifteen
seconds, or `ALIVE_NO_PROGRESS` when heartbeats continue but its stage has not
advanced for thirty seconds. Inspect without changing factory state:

```bash
python3 factory/runtime/status.py --run-dir factory/runtime/logs/current
```

One delivery attempt uses one trace ID derived from the repository, Story, and
latest durable `story:claimed` timestamp. The dispatcher, poller, delivery
worker, independent reviewer, and merge gate re-derive that same ID from GitHub;
no in-memory handoff is required.

### Exact-head merge routing

The poller never enables persistent GitHub auto-merge for a factory delivery.
After independent review approves the current head and GitHub reports required
checks green, it requests a squash merge with `--match-head-commit HEAD_SHA`.
That comparison is enforced by GitHub at the merge write: if any rebase or push
changed the head, the merge is rejected and the new head requires a new review.
If checks are still pending, the PR stays open and a later poll retries. The
poller also disables legacy auto-merge settings before routing open factory PRs.

A factory-launched worker used to be observable only through whatever `[worker]`
line reached stdout, and `workers.launch` kept a *launched* worker's stdout and
a *failed* worker's stderr — so for any run that hung, the one channel with the
explanation in it was the one thrown away. Diagnosing a hang meant re-running
the engine by hand.

`runlog.py` is the record. One JSON object per line, appended to
`factory/runtime/logs/runtime.jsonl` (override with `FACTORY_RUNTIME_LOG`) and
mirrored to stderr unless `FACTORY_RUNTIME_LOG_STDERR=0`.

**Never stdout.** Under this repository's monitor one stdout line is one
notification, so a chatty log there would page a human per event.

| Event | Answers |
|---|---|
| `dispatch.received` | what the dispatcher authorized, and for which project |
| `worker.health` / `worker.selected` | which engines were eligible, and why |
| `worker.launch.start` | the exact command, and the timeout it runs under |
| `process.started` | the child's PID — which process to look at in `ps` |
| `worker.launch.end` | exit status, elapsed ms, **both** output streams |
| `worker.failover` | fell back, suppressed, or not needed — with the reason |
| `worker.outcome` | which engine ended up owning the Story |
| `bridge.dispatch` / `bridge.engine.exit` | the engine's own view of the same run |
| `bridge.acknowledgement` | `PRESENT` / `ABSENT` / `UNVERIFIABLE` |
| `bridge.outcome` | the verdict, and the sentence that justifies it |
| `story.completion` | what the completion path decided, and whether it wrote |

Every record carries a `run` id, so events from a poll and from the bridge
subprocess it launched stitch back together.

**Two things are deliberately absent.** *Secrets*: any occurrence of the live
`GITHUB_TOKEN` / `GH_TOKEN` is removed by value before a line is written, and
token-shaped strings are removed by pattern — a worker that echoes its
environment cannot spill a credential into the log. *Hidden model reasoning*:
only what a process actually wrote to stdout/stderr is kept, tailed to
`MAX_FIELD_CHARS`; nothing asks an engine for its chain of thought.

**Logging never costs anything.** Every write is best-effort and every error is
swallowed — a logging failure that stopped a dispatch would be a logging system
that costs more than it earns. The file rotates once at `MAX_LOG_BYTES`, because
a long-running loop appending forever is a loop without a bound.

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

`poll.sh` in the repository root is the operator's entry point — it supplies the
repository, the commitment and a token, and passes everything else through:

```sh
./poll.sh --once              # one cycle and exit
./poll.sh --once --dry-run    # decide, write nothing
./poll.sh                     # watch continuously (this is the service)
```

It holds **no policy**, and that is a constraint rather than an observation.
Authorization, eligibility, WIP, lifecycle and recovery live in GitHub (§9.12)
and in the modules the poller invokes. A rule that appears in a wrapper has two
sources and only one of them is the system of record.

The underlying commands are unchanged:

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
stdout stream: every poll that dispatches prints a `[poller]` line, every
completion prints a `[completion]` line, and every failure prints one too.

For anything more detailed than that — which engine ran, what it printed, why a
verdict was reached — read the structured log:

```sh
tail -f factory/runtime/logs/runtime.jsonl | python3 -m json.tool --json-lines

# just one story's history, across polls and bridge subprocesses
grep '"story":103' factory/runtime/logs/runtime.jsonl
```

## Tests

```sh
cd factory/runtime && python3 -m unittest discover -p 'test_*.py' -v
```

Standard library only. The parsing tests carry the weight — the near-miss cases
matter more than the happy path.

`test_lifecycle_e2e.py` is the one that wires the real dispatcher, the real
worker contract and the real completion path together against one in-memory
GitHub and asserts on durable state: a Story goes `ready → claimed → worker →
completed + closed` in a single poll, and no later poll or restart launches a
second worker for it. It keeps the counterfactual next to it — a worker that
proves nothing leaves the claim standing for §9.4 — so the fix is read as the
answer to something real.
