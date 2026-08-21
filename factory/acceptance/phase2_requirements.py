#!/usr/bin/env python3
"""The **Phase 2** requirements, and what an end-to-end run can honestly reach.

Named for its phase on purpose. An earlier name — `e2e_requirements` — read as
though it held every requirement there would ever be, which is an invitation for
someone reaching Phase 4 to append to it. Phase 2's requirements are Phase 2's;
a later phase gets its own file, and the difference will matter more than it
looks, because the later phases are not all this shape.

`implementation-plan-v1.md` puts it plainly for Phase 3: *"is the plan digest one
you would sign?"* That is a human judgement with no test to have, and a coverage
report demanding one for it would be measuring the wrong thing loudly. Phase 4 —
one toy story through the full loop with no manual step — does decompose like
this. Phase 5 is KPI measurement, different again.

So the *mechanism* here generalises to some phases and not others, and the
honest time to extract it is when a second real instance exists to extract it
against, not now on a guess about the shape of one.

This file exists so that end-to-end coverage is a **statement** rather than an
impression. `e2e.py` prints it on every run, including the requirements no
scenario covers — because a suite that lists only what it does prove reads as
complete, and the reader has no way to discover what is missing.

Every entry names the requirement, the §-reference that defines it, and the tier
its live scenario belongs to. A requirement with no scenario carries the reason
and what would unlock it.

## The tiers, and why residue decides them

`DEFAULT` — cheap and **self-cleaning**. Every fixture it creates reaches a
terminal state through the factory's own completion path. Safe to run in a loop.

`OPT_IN` — correct, but leaves a mark: an open issue that only a human may
dispose of, or a commit on `main`. Never runs unless asked for by name. The
distinction is not fussiness: an end-to-end suite that quietly accumulates open
issues teaches its operators to ignore open issues.

`DEFERRED` — needs elapsed wall-clock time and therefore two invocations. The
fixture issue carries the state between them, so nothing local has to survive.

`UNREACHABLE` — cannot be produced by this credential against this repository at
all. Two of these exist and both are honest limits rather than gaps in effort.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT = "default"
OPT_IN = "opt-in"
DEFERRED = "deferred"
UNREACHABLE = "unreachable"

TIERS = (DEFAULT, OPT_IN, DEFERRED, UNREACHABLE)


@dataclass(frozen=True)
class Requirement:
    key: str
    behaviour: str
    reference: str
    tier: str
    note: str = ""

    @property
    def covered(self) -> bool:
        """Reachable live at all — whether or not this particular run ran it."""
        return self.tier != UNREACHABLE


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "dispatch", "deterministic dispatch — order, WIP, atomic claim", "§9.10", DEFAULT,
        "three fixtures; the dispatcher claims WIP_LIMIT of them in (project, story) order"),
    Requirement(
        "authorization", "authorization chain, each link refused by name", "§9.9, §3.2", DEFAULT,
        "one fixture mutated link by link, ending valid so it completes and cleans itself up"),
    Requirement(
        "trust-boundary", "an untrusted author cannot cause a dispatch", "§9.9", DEFAULT,
        "covered live in the strongest form one identity allows. A public repository "
        "accumulates artifacts from non-collaborators, and this one has: GitHub stamped a "
        "real comment `author_association: NONE`, which this credential cannot forge. The "
        "scenario refuses that real artifact through the production check and proves the "
        "rule reads live data. What it CANNOT do is open an untrusted *story* and watch the "
        "dispatcher refuse it — that needs an account which is not a collaborator, and #27 "
        "chose a single identity. Acceptance scenario S3 covers that half; a second "
        "identity (#26) would close it"),
    Requirement(
        "scope-gate", "the required gate is enforced and unbypassable", "§9.6, §9.14", DEFAULT,
        "asserts the enforcement surface from live repository configuration, which §9.14 "
        "names as a trust input precisely because the credential cannot fabricate it. The "
        "red-on-violation behaviour runs on every real pull request already; manufacturing "
        "an extra failing PR per run would add noise to prove what the last dozen proved"),
    Requirement(
        "worker-launch", "selection follows configuration; the bridge launches", "§4, #84", DEFAULT),
    Requirement(
        "observability", "the run is reconstructable from the log alone", "#104", DEFAULT),
    Requirement(
        "completion", "a proven bounded success reaches story:completed", "§9.16", DEFAULT),
    Requirement(
        "review-open", "an open §9.5-linked pull request moves a claim to review", "§4.2, §9.11", DEFAULT,
        "attested from durable history rather than caused: causing it live means a real "
        "pull request per run"),
    Requirement(
        "review-merged", "a merged delivery closes the story as story:merged", "§4.2, §9.11", DEFAULT,
        "attested from durable history — #122, #124 and #126 each walked it with every "
        "label written by a component. Causing it live would add a commit to `main` per "
        "run to re-prove what the history already records"),
    Requirement(
        "dependencies", "a closed terminal-success dependency satisfies Depends-on", "§9.10, §9.3", DEFAULT,
        "the case #107 fixed: a hermetic suite proved the rule while production rejected "
        "every satisfied dependency, because both terminal successes close the issue"),
    Requirement(
        "replay", "a replay poll changes nothing", "§9.10, §9.15", DEFAULT),
    Requirement(
        "recovery", "an expired claim recovers and restores its attempt", "§9.4", DEFERRED,
        "`CLAIM_LEASE` is sixty minutes measured from a durable `story:claimed` timeline "
        "event, and timeline events cannot be backdated. One process cannot reach it; two "
        "invocations an hour apart can, with the fixture issue carrying the state"),
    Requirement(
        "failover", "a definite engine failure falls back; ambiguity never does", "#84", DEFAULT,
        "the primary engine is pointed at a binary that does not exist, which is a definite "
        "failure rather than an ambiguous one"),
    Requirement(
        "attempt-limit", "the threshold poisons instead of dispatching, and stays open", "§4.3.5, §9.3", OPT_IN,
        "a poisoned story waits for a human by design, so this cannot self-clean. Reuses an "
        "existing open poison fixture when one is present, bounding residue at one issue"),
    Requirement(
        "fail-closed", "no credential, malformed input and unreachable state all fail closed", "§9.1", DEFAULT),
)

BY_KEY = {requirement.key: requirement for requirement in REQUIREMENTS}


def summary() -> dict:
    counts = {tier: 0 for tier in TIERS}
    for requirement in REQUIREMENTS:
        counts[requirement.tier] += 1
    reachable = sum(1 for r in REQUIREMENTS if r.covered)
    return {
        "total": len(REQUIREMENTS),
        "reachable": reachable,
        "unreachable": len(REQUIREMENTS) - reachable,
        "by_tier": counts,
    }
