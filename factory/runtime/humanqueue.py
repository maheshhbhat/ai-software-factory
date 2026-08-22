#!/usr/bin/env python3
"""Human queue — what is waiting on a person, said out loud on every poll.

Phase 2 Closeout (#111). §9.11 ends with a rule the factory did not keep:

> **No silent drops.** A transition with no live route is logged and surfaced,
> never discarded — an unrouted transition is a contract gap, and silence would
> hide it.

The silence was real and it was measurable. `continuation.run` skips a project
whose outcome is `NO_DECISION` without printing anything, so a project sitting
at `project:awaiting-ready` with no approval produced **no output at all**, on
every poll, indefinitely — which is how #55, #61 and #66 each sat waiting until
somebody happened to look. A story at `story:blocked:scope` appeared only as a
dispatcher skip line among the ineligible. A poisoned story appeared nowhere,
because until #110 poisoning closed the issue and the queue is open issues.

This module is the answer, and it is deliberately the dumbest thing that works:
**enumerate everything waiting, every poll, from durable state.**

## Why it holds no state, and why that is the design

Nothing here records that an artifact was announced. The list is recomputed from
GitHub on each pass, so an artifact that is still waiting is announced again, and
one that stopped waiting stops being announced — automatically, with no cursor
to go stale and nothing to reconcile after a restart.

A notifier that remembers what it has already said is a notifier that goes quiet
about a problem precisely because the problem is old. That is the failure mode
this exists to prevent, so "already notified" is not a state this module can be
in.

## What it emits, and what it deliberately does not

One canonical stdout line per waiting artifact, carrying identity, state, link
and the action required — and one structured runlog event with the same fields.
The line is a wake-up for a human, not a payload: like the dispatcher's
`DISPATCH` line it names the artifact rather than describing it, because the
moment a queue item copies business context the relay has been rebuilt as
infrastructure (`architecture-v2.1.md` §4).

The transport is the monitor that already turns one stdout line into one
notification. No webhook, no secret, no outbound dependency — Phase 2 gets a
channel it can prove, not one it has to configure.

## The trust boundary here runs the other way

§9.9 says untrusted issues are readable but never authorizing. A queue entry
authorizes nothing — it asks a human to look — so an untrusted artifact is
listed rather than hidden, and marked. Hiding it would be the worse error:
§9.9's own words are that untrusted text "may be *read* — for context, for a
human's queue".
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "dispatcher"))
sys.path.insert(0, os.path.join(HERE, "..", "gates"))

import dispatcher  # noqa: E402 — the lifecycle vocabulary is defined once, there
import runlog  # noqa: E402

# §9.11's last row, plus the action each one is waiting for. The state is the key
# because the state is what a human can see and change; the action is prose for a
# person, and it is the only prose in this module.
#
# **Each action names the exact recordable form, not merely the decision** (#122).
# The queue used to say "post a `## Plan approval` comment" and stop there. On
# #109 the CTO did exactly that, wrote `APPROVED.`, and the continuation pass
# refused it: §5.1 declares a machine-readable shape carrying a literal
# `decision:` line, and the queue had not said so. The parser was right — reading
# prose to decide whether the factory is authorized is what §9.9 forbids — so the
# defect was here, in the instruction. A queue that tells a person *what* to
# decide but not *how to record it* manufactures a relay touch every time, and
# relay is the one metric this phase is measured on driving to zero.
#
# `HUMAN_QUEUE_FORMATS` below pins each of these strings against the regex that
# will actually read the reply. A fixed string alone would not prevent this
# recurring: the failure mode is *drift*, an instruction and a parser that stop
# agreeing, and only a test that reads both can catch that.
#
# **An action must also not promise behaviour no component performs** (#181). The
# poison entry used to end "or leave it and let §4.3.7 close it as not planned",
# which is false: `dispatcher.poison_closure` closes a poisoning only once the
# §4.3.7 rescue cap is spent, and spending it takes two rescues. A story left
# alone is never closed by anything — it stays open and is reported every poll,
# which is §9.3 working as written. The entry also omitted the disposition that
# *is* available, §9.4.1's cancellation, so the one route a finished or abandoned
# Story needs was the one route the queue never mentioned.
#
# `HUMAN_QUEUE_FORMATS` cannot catch this class: there is no parser for "leave
# it", because the promise is about a component's future behaviour rather than a
# comment's shape. `test_humanqueue.py` therefore drives `poison_closure` itself
# and asserts the text agrees with what it returns.
WAITING_ON_A_HUMAN: dict[str, str] = {
    "project:awaiting-ready":
        "approve the falsifiable acceptance criteria, or request changes — post a "
        "`## Plan approval` comment containing the line `decision: approved` "
        "(or `decision: changes-requested`) and quoting the criteria verbatim (§5.1)",
    "project:awaiting-acceptance":
        "record outcome acceptance — post an `## Acceptance` comment containing the "
        "line `result: pass` (or `result: fail`) and one `- <criterion> — pass/fail` "
        "line per criterion (§5.3)",
    dispatcher.POISON:
        "decide the disposition — §9.4.1 offers two, and doing nothing is not one "
        "of them. Rescue per §4.3.6 if the failure was infrastructural: all three, "
        "in order, a rescue comment stating what changed, `### Attempt` reset to "
        "`0` in the body, and one `poison-rescue` touch logged. Or cancel per §9.3 "
        "if the work should stop: apply `story:cancelled` and close as not planned, "
        "which is a human decision no component may take. Left alone this story "
        "stays open and is reported again every poll — §4.3.7 closes a poisoning "
        "only once the rescue cap of 2 is spent, which takes two rescues first",
    "story:blocked:scope":
        "resolve the scope dispute — amend `### Scope` or withdraw the dispute, move "
        "the label, and log one `scope-decision` touch (§4.2). No automated pass "
        "consumes this decision, so there is no comment format to match",
}

# The literal each action promises, against the pattern that will read it. Pinned
# in a test so an instruction and its parser cannot drift apart in silence — see
# `test_humanqueue.py`. `None` means no automated consumer, which is a fact about
# the route worth stating rather than a gap to fill with an invented format.
HUMAN_QUEUE_FORMATS: dict[str, tuple[str, str] | None] = {
    "project:awaiting-ready": ("decision: approved", "## Plan approval"),
    "project:awaiting-acceptance": ("result: pass", "## Acceptance"),
    dispatcher.POISON: None,          # §4.3.6 is a three-part human act, not a parse
    "story:blocked:scope": None,      # §4.2 resolution is read by no component
}

PREFIXES = (dispatcher.STORY_LIFECYCLE, dispatcher.PROJECT_LIFECYCLE)

SAMPLING_ACTION = (
    "audit the sampled merged PR — post `## Sampling audit` with exactly one "
    "`decision: pass` (or `decision: findings`) line, `seconds-spent: N`, and "
    "for findings one or more `- actionable finding` lines")


@dataclass(frozen=True)
class Waiting:
    number: int
    state: str
    action: str
    url: str
    title: str
    trusted: bool

    def line(self) -> str:
        """One canonical line. Identity, state, link, action — nothing else.

        Parseable on purpose: the same discipline as the dispatcher's `DISPATCH`
        line, so a future consumer never has to read prose to find the artifact.
        """
        mark = "" if self.trusted else " trust=untrusted"
        return (f"HUMAN-QUEUE artifact=#{self.number} state={self.state} "
                f"url={self.url}{mark} action={self.action!r}")


def state_of(issue: dict) -> str | None:
    """The single lifecycle label, whichever family it belongs to.

    Two labels is ambiguous, not "the first" — the same rule the dispatcher
    applies, and an artifact whose lifecycle cannot be read is reported as
    ambiguous rather than assigned a state it may not be in.
    """
    for prefix in PREFIXES:
        found = sorted(x for x in dispatcher.labels_of(issue) if x.startswith(prefix))
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def waiting_for(issue: dict) -> Waiting | None:
    """Is this artifact waiting on a person? Pure."""
    state = state_of(issue)
    action = WAITING_ON_A_HUMAN.get(state or "")
    if (action is None and "type:audit" in dispatcher.labels_of(issue)
            and "<!-- sampling-audit:" in (issue.get("body") or "")
            and issue.get("state", "open") == "open"):
        state, action = "sampling-audit", SAMPLING_ACTION
    if action is None:
        return None
    return Waiting(
        number=issue.get("number", 0),
        state=state,
        action=action,
        url=issue.get("html_url") or "",
        title=(issue.get("title") or "").strip(),
        trusted=dispatcher.is_trusted(issue),
    )


def waiting(issues: dict) -> list[Waiting]:
    """Everything waiting on a human, in issue order. Deterministic (§9.10)."""
    found = (waiting_for(issues[number]) for number in sorted(issues))
    return [item for item in found if item is not None]


def render(items: list[Waiting]) -> list[str]:
    """The lines one pass emits. The summary is emitted even when the queue is
    empty: "nothing is waiting" is a fact worth stating once per poll, and its
    absence would be indistinguishable from the pass not having run."""
    if not items:
        return ["[human-queue] nothing is waiting on a human"]
    lines = [f"[human-queue] {len(items)} artifact(s) waiting on a human"]
    lines.extend(item.line() for item in items)
    return lines


def run(repo: str, token: str) -> list[Waiting]:
    """One pass. Reads durable state, says what is waiting, writes nothing.

    This pass never mutates a lifecycle. An artifact waiting on a human is
    waiting *because* no component may decide it, and a notifier that could
    change what it reports on would not be a notifier.
    """
    items = waiting(dispatcher.fetch_issues(repo, token))
    for line in render(items):
        print(line, flush=True)
    for item in items:
        runlog.event("human_queue.waiting", artifact=item.number, state=item.state,
                     url=item.url, action=item.action, title=item.title,
                     trusted=item.trusted)
    runlog.event("human_queue.summary", waiting=len(items), repo=repo)
    return items
