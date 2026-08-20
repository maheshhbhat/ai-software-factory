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

## Tests

```sh
cd factory/dispatcher && python3 -m unittest discover -p 'test_*.py' -v
```

Standard library only. `TestTrustBoundary` holds the security cases — a
stranger's issue with correct labels and urgent prose, an untrusted project
parent, approval-sounding body text — and asserts none of them dispatch.
