# Dispatcher

Reads durable GitHub state, decides which stories are authorized and eligible,
claims up to `WIP_LIMIT` of them, and emits a dispatch line the local monitor
turns into a worker wake-up. Implements `factory/spec/state-schema.md` §9.9–§9.12.

**The relay it deletes:** before this, a human read a new issue number and typed
`work #N` to start authorized work. That touch carried no judgment — the label
already was the decision — which makes it a *relay* touch, the one class
`architecture-v2.1.md` §7 requires to trend to zero.

## What authorizes work

A collaborator-set lifecycle label on an artifact whose **whole chain** checks
out:

```
story  ── ### Project ──▶  project  ── ### Roadmap commitment ──▶  commitment
type:story                 type:project                            standing
story:ready                project:active
collaborator-authored      collaborator-authored
```

plus satisfied `### Depends-on`, a §9.6-parseable `### Scope`, and
`Attempt < 3`. Any broken link means no dispatch and a named reason.

Never authorizing: issue creation, body prose, comments, `Agent-ID`, labels on
an untrusted author's issue. On a public repository anyone can open an issue and
write persuasive text; none of it reaches a code path.

## Rejection reasons

`NOT_A_STORY` · `UNTRUSTED_AUTHOR` · `ISSUE_CLOSED` · `AMBIGUOUS_LIFECYCLE` ·
`NOT_READY` · `PROJECT_LINK_MISSING` · `PROJECT_LINK_MALFORMED` ·
`PROJECT_NOT_FOUND` · `PROJECT_WRONG_TYPE` · `PROJECT_UNTRUSTED_AUTHOR` ·
`PROJECT_NOT_ACTIVE` · `COMMITMENT_LINK_MISSING` · `COMMITMENT_MISMATCH` ·
`DEPENDENCY_UNMET` · `DEPENDS_ON_MALFORMED` · `SCOPE_INVALID` ·
`ATTEMPT_INVALID` · `ATTEMPT_EXHAUSTED`

Every rejection is attributable. A story that does not run must say why, or the
dispatcher is unauditable.

## Selection and claiming

Deterministic (§9.10): eligible stories ordered by *(project number, story
number)*, taken until `WIP_LIMIT = 2` claimed stories exist. Issue numbers are
unique, so the ordering is total — no ties, no randomness, same input, same
output.

A claim re-reads the story immediately before writing, then replaces the
**entire** label set in one `PATCH` and increments `Attempt` by exactly 1
(§9.2, §4.3.2). Two concurrent polls cannot both claim: the second finds the
story no longer `story:ready` and declines. The state write is the duplicate
suppressor — no lock, no cursor, nothing authoritative outside GitHub.

## Running it

```sh
# dry run — decides and explains, writes nothing
GITHUB_TOKEN=... python3 factory/dispatcher/dispatcher.py \
    --repo owner/name --commitment 54

# claim and dispatch
GITHUB_TOKEN=... python3 factory/dispatcher/dispatcher.py \
    --repo owner/name --commitment 54 --claim

# offline, against a JSON fixture
python3 factory/dispatcher/dispatcher.py --fixture path.json
```

Dispatch lines carry identity only:

```
DISPATCH story=#42 project=#901 agent=claude-delivery
```

No spec, no scope, no business context — the worker reads the substrate itself
(`architecture-v2.1.md` §4: the moment a queue item copies business context, the
relay has been rebuilt as infrastructure).

## The recovery bound (§9.4.1)

Recovery restores the attempt that dispatch consumed. Without a cap that makes
`(dispatch, expiry)` an infinite cycle — `ready Attempt 0 → claimed Attempt 1 →
expiry → ready Attempt 0 → …` — in which the counter never advances, poison is
never reached, and a worker is woken every lease period forever. Story #64 was a
live instance.

`RECOVERY_MAX = 2`. Recoveries are counted from the issue timeline: a
`story:claimed → story:ready` transition with no `story:in-review` between them
is an expiry recovery; a findings-driven return passes through review and does
not count. Nothing is stored locally. On the next expiry after the budget is
spent, the story goes to `story:blocked:poison` with `RECOVERY_BUDGET_EXHAUSTED`
rather than recovering again.

**The bound as a number: at most `RECOVERY_MAX + 1` = 3 dispatches** through the
expiry path before a human is asked.

The policy is deliberately dumb. Durable evidence cannot distinguish a worker
that died before starting from one that ran correctly and produced no PR — both
leave an expired claim and no pull request. Rather than infer which happened,
the dispatcher recovers a fixed number of times and then stops. The human's
options are named in the poison reason: rescue per §4.3.6 if the failure was
infrastructural, or cancel per §9.3 if the story legitimately has no deliverable.

## Boundaries of this increment

Implemented: authorization chain, eligibility, deterministic selection, atomic
claim, dispatch trigger.

Before WIP selection the claim-recovery pass reconciles durable evidence:

* a claim younger than the 60-minute §9.4 lease remains claimed;
* an expired claim with no §9.5-linked PR returns to `story:ready` and restores
  the pre-dispatch Attempt value;
* one mechanically linked merged PR closes the story as `story:merged`;
* missing, duplicate, or contradictory evidence fails closed with a named bell.

The dispatcher then re-fetches GitHub state and calculates WIP. Replaying a
completed transition is a no-op because only `story:claimed` is recoverable.

Also out of scope: worker execution, reviewer, notifications, auto-merge.

## Observed monitor gap

Project #55 already had an owner-authored plan approval, but generic `poll` and
`poll #55` did not consume it; a manual `work #55` relay was needed to move
`project:awaiting-ready` to `project:active`. Claim recovery does not broaden
into general bell consumption. Follow-up: make the monitor reconcile approved
project bells before routing, using the same durable-evidence and idempotency
rules as this recovery pass.

## The §9.15 replay (`replay.py`)

`state-schema.md` §9.15 makes a replay of Phase 1's recorded history a
precondition for the dispatcher writing anything, and `implementation-plan-v1.md`
names the same replay as the proof that dispatch happens once and only once per
transition. It replays the `labeled` / `unlabeled` timelines of #1, #10, #11 and
#12 — the walk verified under #19, chosen because it contains the retry
sequence, the poison threshold, the rescue, and the scope detour rather than
only the happy path.

```bash
python3 factory/dispatcher/replay.py                      # replay the captured fixtures
GITHUB_TOKEN=… python3 factory/dispatcher/replay.py \
    --capture --repo owner/name                            # re-read the history, GET only
```

Three properties, and each clause earns its place. **Exactly once** — every
reconstructed transition resolves to one routing decision, because a transition
routed twice is a duplicate dispatch and one routed zero times is work that
never starts. **No silent drops** — a route that is documentation-only until a
later phase is reported by name, and a transition the table does not recognise
is a failure, since §9.11 calls an unrouted transition a contract gap.
**Read-only** — the replay writes to no issue, and `--capture` uses GET alone.

A transition is reconstructed statefully, not by pairing events by timestamp.
The recorded history predates §9.2 being enforced and shows it: on #10 the
`story:claimed` label was removed at 20:24:22 and `story:in-review` applied at
20:24:23, leaving a one-second window with no lifecycle label. Pairing by
timestamp would read that as two transitions and invent a state the factory was
never in. The replay instead applies events in order and emits a transition only
when the label set settles on one label different from the last — so the window
is recorded as a §9.2 finding, which is what it is. The replay rediscovers 33 of
them across the four issues, which is the same defect the Phase 1 repair
recorded, derived independently from the raw events.

## Poison closure and the rescue cap (§4.3.7, §9.3)

A poisoned story is **open**. §9.3 closes `story:blocked:poison` only after
§4.3.7's rescue cap is spent, and until then the story is waiting for a human —
a closed issue waits for nobody. Closing on the first poisoning, which this
module did until #110, also put the story beyond reach of the §4.3.6 rescue,
because §9.3 permits only a human to reopen an issue and no component may.

Rescues are counted from the issue timeline, the same technique
`count_expiry_recoveries` uses and for the same reason: durable history is the
record. Both paths that reach poison — the §4.3.5 dispatch threshold and the
§9.4.1 recovery budget — apply the identical closure rule, so the terminal state
does not mean two different things depending on how the story arrived at it.

## Tests

```sh
cd factory/dispatcher && python3 -m unittest discover -p 'test_*.py' -v
```

Standard library only. `TestTrustBoundary` holds the security cases — a
stranger's issue with correct labels and urgent prose, an untrusted project
parent, approval-sounding body text — and asserts none of them dispatch.
