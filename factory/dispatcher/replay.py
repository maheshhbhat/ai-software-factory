#!/usr/bin/env python3
"""§9.15 replay acceptance test — every recorded transition routes exactly once.

`state-schema.md` §9.15 makes this a precondition, in its own words: *"Before the
dispatcher is permitted to write anything, it must pass a replay of Phase 1's
recorded history."* `implementation-plan-v1.md` names the same replay as the
proof that dispatch happens **once and only once per transition**, so Phase 2's
stated verification cannot be produced without it.

The input is durable history nobody can retouch: the `labeled` / `unlabeled`
timeline events of issues #1, #10, #11 and #12, produced by the Phase 1 walk
verified under #19. That walk deliberately included the exception paths — the
retry sequence, the poison threshold, the human rescue, and the scope detour — so
a replay over it exercises the branches that matter and not merely the happy one.

What this asserts, and why each clause is there:

* **Exactly once.** Every reconstructed transition resolves to exactly one
  routing decision. A transition routed twice is a duplicate dispatch; a
  transition routed zero times is work that silently never starts.
* **No silent drops.** A transition whose route is documentation-only until a
  later phase is reported as *unrouted*, by name. A transition the table does not
  know at all is a **failure**, not a shrug — §9.11 says an unrouted transition
  is a contract gap, and silence would hide it.
* **Restart safety.** Replay from an empty cursor produces identical decisions,
  which is what "no authoritative state lives outside GitHub" means operationally.
* **Read-only.** The replay asserts decisions the dispatcher *would* make. It
  writes to no issue, and `--capture` is the only code path here that touches the
  network at all, using GET alone.

## Reconstructing a transition from events

A transition is a change of the single lifecycle label, and §9.2 requires it to
be written as one atomic label-set replacement. The recorded history predates
that rule being enforced, and it shows: on #10 the `story:claimed` label was
removed at 20:24:22 and `story:in-review` applied at 20:24:23, leaving a
one-second window with **no lifecycle label at all**. That window is the reason
§9.2 exists (#19), and a replay that paired events by timestamp would read it as
two transitions — `claimed → ∅` and `∅ → in-review` — inventing a state change
that never happened.

So the walk is stateful rather than positional: events are applied in
chronological order to a running label set, and a transition is emitted only when
that set **settles on exactly one lifecycle label different from the last settled
one**. A momentary zero-label or two-label state is a transient; it is counted
and reported, because it is a real §9.2 finding, but it is never a transition.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "gates"))

import dispatcher  # noqa: E402 — the lifecycle vocabulary is defined once, there
import merge_gate  # noqa: E402 — label_name, likewise

DEFAULT_ISSUES = (1, 10, 11, 12)   # §9.15 names these four, and only these
FIXTURE_DIR = os.path.join(HERE, "fixtures", "phase1")

STORY = dispatcher.STORY_LIFECYCLE
PROJECT = dispatcher.PROJECT_LIFECYCLE

# Routing statuses. `UNKNOWN` is not a status a route may declare — it is what
# the replay reports when the table has nothing to say, and it always fails.
LIVE = "live"                 # a Phase 2 route exists and runs today
DEFERRED = "deferred"         # documentation-only until its phase arrives (§9.11)
HUMAN = "human"               # only a human performs it; no component may
CREATION = "creation"         # an issue coming into existence is not a transition
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Route:
    status: str
    action: str


# §9.11 is the source of this table; §4.1 and §4.2 supply the actor column, and
# §9.16 the completion route added after both. Keyed by (from, to), where `None`
# is "the issue did not exist yet".
#
# Two routes are marked DEFERRED here and are promoted to LIVE by story #111,
# which is the increment that implements them. That is deliberate: the table
# states what the factory *does*, not what it intends to do, so a route becomes
# live in this table on the commit that makes it live in the world.
ROUTES: dict[tuple[str | None, str], Route] = {
    # ---- story (§4.2) -----------------------------------------------------
    (None, "story:blocked"): Route(CREATION, "issue created; §9.12 — creation authorizes nothing"),
    (None, "story:ready"): Route(CREATION, "issue created; §9.12 — creation authorizes nothing"),
    ("story:blocked", "story:ready"): Route(DEFERRED, "sequencer marks deps satisfied (Phase 3)"),
    ("story:ready", "story:claimed"): Route(LIVE, "dispatch a worker, increment Attempt (§4.3.2)"),
    ("story:claimed", "story:ready"): Route(LIVE, "claim lease expiry, restore Attempt (§9.4)"),
    ("story:claimed", "story:in-review"): Route(DEFERRED, "PR opened referencing the story (§9.5)"),
    ("story:claimed", "story:completed"): Route(LIVE, "bounded success with nothing to merge (§9.16)"),
    ("story:in-review", "story:merged"): Route(DEFERRED, "delivery PR merged (gated on §9.13)"),
    ("story:in-review", "story:ready"): Route(DEFERRED, "review findings returned (Phase 4)"),
    ("story:ready", "story:blocked:poison"): Route(LIVE, "attempt threshold reached; notify, do not dispatch (§4.3.5)"),
    ("story:claimed", "story:blocked:poison"): Route(LIVE, "recovery budget exhausted; notify (§9.4.1)"),
    ("story:blocked:poison", "story:ready"): Route(HUMAN, "poison rescue (§4.3.6)"),
    ("story:ready", "story:blocked:scope"): Route(HUMAN, "scope dispute raised (§4.2)"),
    ("story:claimed", "story:blocked:scope"): Route(HUMAN, "scope dispute raised (§4.2)"),
    ("story:blocked:scope", "story:ready"): Route(HUMAN, "scope dispute resolved and unblocked (§4.2)"),
    ("story:blocked:scope", "story:blocked"): Route(HUMAN, "scope dispute resolved, still blocked (§4.2)"),
    ("story:ready", "story:cancelled"): Route(HUMAN, "work deliberately stopped (§9.3)"),
    ("story:claimed", "story:cancelled"): Route(HUMAN, "work deliberately stopped (§9.3)"),
    # ---- project (§4.1) ---------------------------------------------------
    (None, "project:queued"): Route(CREATION, "issue created; §9.12 — creation authorizes nothing"),
    (None, "project:awaiting-ready"): Route(CREATION, "issue created; §9.12 — creation authorizes nothing"),
    ("project:queued", "project:ready-for-planning"): Route(DEFERRED, "planning becomes eligible (Phase 3)"),
    ("project:ready-for-planning", "project:planning"): Route(DEFERRED, "planning agent invoked (Phase 3)"),
    ("project:planning", "project:awaiting-ready"): Route(DEFERRED, "planning output written (Phase 3)"),
    ("project:awaiting-ready", "project:active"): Route(LIVE, "consume the §5.1 plan approval"),
    ("project:awaiting-ready", "project:planning"): Route(DEFERRED, "changes requested; re-plan (Phase 3)"),
    ("project:active", "project:awaiting-ready"): Route(HUMAN, "criteria amended; approval superseded (§5.2)"),
    ("project:active", "project:awaiting-acceptance"): Route(DEFERRED, "every story terminal; sequencer (Phase 3)"),
    ("project:awaiting-acceptance", "project:accepted"): Route(LIVE, "consume the §5.3 acceptance, pass"),
    ("project:awaiting-acceptance", "project:active"): Route(LIVE, "consume the §5.3 acceptance, criterion failed"),
}

# §9.11, last row: entering any of these rings the human queue, whatever route
# brought the artifact there. Orthogonal to the route itself — a transition can
# be deferred *and* ring a bell, which is exactly the `planning → awaiting-ready`
# case, and reading the two as one thing is how a bell gets lost.
HUMAN_QUEUE_STATES = frozenset({
    "project:awaiting-ready",
    "project:awaiting-acceptance",
    "story:blocked:poison",
    "story:blocked:scope",
})


@dataclass(frozen=True)
class Transition:
    issue: int
    at: str
    frm: str | None
    to: str


@dataclass(frozen=True)
class Routing:
    transition: Transition
    route: Route
    rings_human_queue: bool

    @property
    def routed(self) -> bool:
        return self.route.status == LIVE


@dataclass
class Report:
    routings: list[Routing] = field(default_factory=list)
    transients: list[tuple[int, str, tuple[str, ...]]] = field(default_factory=list)
    events_seen: int = 0

    @property
    def unknown(self) -> list[Routing]:
        return [r for r in self.routings if r.route.status == UNKNOWN]

    @property
    def live(self) -> list[Routing]:
        return [r for r in self.routings if r.route.status == LIVE]

    @property
    def unrouted(self) -> list[Routing]:
        return [r for r in self.routings if r.route.status in (DEFERRED, HUMAN, CREATION)]

    @property
    def bells(self) -> list[Routing]:
        return [r for r in self.routings if r.rings_human_queue]

    def passed(self) -> bool:
        """Exactly-once holds and every transition is accounted for by name."""
        return not self.unknown and not self.duplicates()

    def duplicates(self) -> list[Transition]:
        """A transition resolved more than once is a duplicate dispatch."""
        seen: dict[tuple, int] = {}
        for routing in self.routings:
            key = (routing.transition.issue, routing.transition.at,
                   routing.transition.frm, routing.transition.to)
            seen[key] = seen.get(key, 0) + 1
        return [Transition(*key) for key, count in seen.items() if count > 1]


def lifecycle_prefix(kind: str) -> str:
    return STORY if kind == "story" else PROJECT


def transitions(issue: int, events: list[dict], prefix: str) -> tuple[list[Transition],
                                                                     list[tuple[int, str, tuple[str, ...]]]]:
    """Reconstruct lifecycle transitions from a timeline. See the module docstring.

    Sorted by `created_at` and then by the event's own index, because several
    events legitimately share a timestamp — §9.2's atomic replacement produces
    exactly that — and a sort that reorders them would fabricate transients.
    """
    ordered = sorted(
        (event for event in events if event.get("event") in ("labeled", "unlabeled")),
        key=lambda item: (item.get("created_at") or "", item.get("_index", 0)))

    live: set[str] = set()
    settled: str | None = None
    found: list[Transition] = []
    transients: list[tuple[int, str, tuple[str, ...]]] = []

    for event in ordered:
        name = merge_gate.label_name(event.get("label", {}))
        if not name.startswith(prefix):
            continue                      # type:*, phase:*, hazard — not lifecycle
        if event.get("event") == "labeled":
            live.add(name)
        else:
            live.discard(name)

        if len(live) == 1:
            current = next(iter(live))
            if current != settled:
                found.append(Transition(issue, event.get("created_at") or "", settled, current))
                settled = current
        else:
            # Zero or two lifecycle labels: a §9.2 violation in flight. Recorded
            # as a finding, never as a state the factory was in.
            transients.append((issue, event.get("created_at") or "", tuple(sorted(live))))

    return found, transients


def route_for(transition: Transition) -> Routing:
    route = ROUTES.get((transition.frm, transition.to),
                       Route(UNKNOWN, "no route declared — §9.11 contract gap"))
    return Routing(transition, route, transition.to in HUMAN_QUEUE_STATES)


def replay(timelines: list[dict]) -> Report:
    """Pure. Same input, same report — that is the restart-safety property."""
    report = Report()
    for record in sorted(timelines, key=lambda item: item["issue"]):
        events = record["events"]
        report.events_seen += len(events)
        found, transients = transitions(
            record["issue"], events, lifecycle_prefix(record.get("type", "story")))
        report.transients.extend(transients)
        for transition in found:
            report.routings.append(route_for(transition))
    return report


# --------------------------------------------------------------------------
# Fixtures — captured read-only, then replayed offline forever after
# --------------------------------------------------------------------------


def load_fixtures(directory: str) -> list[dict]:
    records = []
    for path in sorted(glob.glob(os.path.join(directory, "issue-*.json"))):
        with open(path, encoding="utf-8") as handle:
            records.append(json.load(handle))
    if not records:
        raise FileNotFoundError(f"no issue-*.json fixtures in {directory}")
    return records


def capture(repo: str, issues, token: str, directory: str) -> list[str]:
    """Read the durable timelines and write them down. GET only, never a write.

    Capture exists so the replay can run offline and in CI forever after. The
    fixtures it writes are evidence, not configuration: they are the recorded
    history §9.15 names, and editing one to make a replay pass would be editing
    the evidence.
    """
    os.makedirs(directory, exist_ok=True)
    written = []
    for number in issues:
        issue = dispatcher.fetch_issue(repo, number, token)
        if issue is None:
            raise LookupError(f"issue #{number} does not exist in {repo}")
        labels = dispatcher.labels_of(issue)
        kind = "project" if "type:project" in labels else "story"
        events = [
            {"_index": index, "created_at": event.get("created_at"),
             "event": event.get("event"), "label": {"name": merge_gate.label_name(event.get("label", {}))}}
            for index, event in enumerate(dispatcher.fetch_timeline(repo, number, token))
            if event.get("event") in ("labeled", "unlabeled")
        ]
        path = os.path.join(directory, f"issue-{number}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"issue": number, "type": kind, "repo": repo, "events": events},
                      handle, indent=2, sort_keys=True)
            handle.write("\n")
        written.append(path)
    return written


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(report: Report) -> str:
    lines = [f"§9.15 replay — {report.events_seen} recorded event(s), "
             f"{len(report.routings)} transition(s)", ""]
    for routing in report.routings:
        transition = routing.transition
        frm = transition.frm or "(none)"
        mark = {LIVE: "ROUTED", DEFERRED: "unrouted", HUMAN: "unrouted",
                CREATION: "unrouted", UNKNOWN: "UNKNOWN"}[routing.route.status]
        bell = "  BELL" if routing.rings_human_queue else ""
        lines.append(f"  #{transition.issue:<4} {frm:<28} -> {transition.to:<24} "
                     f"{mark:<9} {routing.route.status:<9} {routing.route.action}{bell}")
    lines.append("")
    lines.append(f"  routed live          {len(report.live)}")
    lines.append(f"  surfaced as unrouted {len(report.unrouted)}")
    lines.append(f"  human-queue bells    {len(report.bells)}")
    lines.append(f"  unknown routes       {len(report.unknown)}")
    lines.append(f"  §9.2 transients      {len(report.transients)} "
                 f"(momentary zero/two-label states in the recorded history)")
    for issue, at, labels in report.transients:
        lines.append(f"      #{issue} {at} -> {list(labels) or 'no lifecycle label'}")
    lines.append("")
    duplicates = report.duplicates()
    if duplicates:
        lines.append(f"FAIL: {len(duplicates)} transition(s) routed more than once — "
                     f"that is a duplicate dispatch.")
        for transition in duplicates:
            lines.append(f"      #{transition.issue} {transition.frm} -> {transition.to}")
    if report.unknown:
        lines.append(f"FAIL: {len(report.unknown)} transition(s) have no declared route. "
                     f"§9.11 calls an unrouted transition a contract gap; silence would hide it.")
        for routing in report.unknown:
            lines.append(f"      #{routing.transition.issue} "
                         f"{routing.transition.frm} -> {routing.transition.to}")
    if report.passed():
        lines.append("PASS: every recorded transition resolved exactly once, and every "
                     "transition without a live route is named rather than dropped.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="§9.15 replay acceptance test")
    parser.add_argument("--fixtures", default=FIXTURE_DIR,
                        help="directory of captured issue-*.json timelines")
    parser.add_argument("--capture", action="store_true",
                        help="re-capture fixtures from GitHub (read-only, GET only)")
    parser.add_argument("--repo", help="owner/name, required with --capture")
    parser.add_argument("--issues", default=",".join(str(n) for n in DEFAULT_ISSUES),
                        help="issue numbers to capture")
    args = parser.parse_args(argv)

    if args.capture:
        if not args.repo:
            parser.error("--repo is required with --capture")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not token:
            print("replay — FAIL: no GITHUB_TOKEN; cannot read durable history.")
            return 1
        issues = [int(n) for n in args.issues.split(",") if n.strip()]
        for path in capture(args.repo, issues, token, args.fixtures):
            print(f"captured {path}")

    report = replay(load_fixtures(args.fixtures))
    print(render(report))

    # Restart safety (§9.15): a second replay from an empty cursor must decide
    # identically. Asserted here rather than only in tests, because the property
    # is about this program's behaviour and not about a helper's.
    again = replay(load_fixtures(args.fixtures))
    if [r.transition for r in again.routings] != [r.transition for r in report.routings]:
        print("FAIL: replay from an empty cursor decided differently on the second run.")
        return 1

    return 0 if report.passed() else 1


def guarded_main(argv: list[str]) -> int:
    """A replay that crashes must not read as a pass."""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — deliberately total
        import traceback
        print(f"replay — FAIL: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(guarded_main(sys.argv[1:]))
