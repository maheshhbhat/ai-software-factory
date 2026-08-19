# AI-Team Delivery Architecture v2.1 — Reviewer Proposal

Response to the v2 design review brief. Verdict: **PROCEED WITH CHANGES.**
The direction is right — GitHub as system of record, skills over standing agents,
authority delegated at the Project boundary, thin supervisor. The changes below are
mostly deletions plus three structural corrections.

---

## 1. The durable mental model

The system is three layers. Everything else in this document elaborates one of them.

| Layer | Contents | Rule |
|---|---|---|
| **Intent and acceptance** (human) | Goals in, outcome judgment out | Humans appear only at intent boundaries and exception branches |
| **Coordination loop** | Router + judgment roles (AI) + mechanical checks (code) | Every role reads state, writes state, exits |
| **Shared state substrate** | Durable state · change feed · identity and access · unforgeable gates · append-only history | Any technology providing these five primitives can host the system |

GitHub is one binding of the substrate: issues/labels = durable state, webhooks =
change feed, GitHub identities = access, branch protection + required status checks =
unforgeable gates, git log + issue timeline = history. The architecture is
substrate-independent; the *assurance level* of the unforgeable-gates primitive is not
free — GitHub is a strong choice precisely because it enforces that property already.

The AI/code boundary is a 2026 snapshot, not architecture. As models improve, work
migrates from judgment roles to mechanical checks; the layers and their interfaces
do not change.

---

## 2. Structural corrections to v2

### 2.1 Resolve the persistence contradiction (§5 vs §2)

v2 §5 requires agents to restart with no conversation memory and reconstruct from
durable artifacts. v2 §2 justifies persistent agents by "continuing ownership and
persistent context." Both cannot be load-bearing. **Adopt §5 fully:** every AI
component is invoked on a state transition, reads what it needs from the substrate,
acts, writes new state, and exits.

Consequence: nothing AI-side is "always present." Heartbeats, liveness monitoring,
execution identity management, and the Supervisor that manages them are solving a
problem this design no longer has.

### 2.2 Reviews trigger on state transitions, never on caller invocation

A reviewer summoned by the reviewee, sharing the reviewee's framing, is not a control.
Review executes when a PR opens (state transition), in a fresh context, seeing only
the diff plus the story spec — never the worker's reasoning. The worker cannot invoke,
skip, re-run, or influence it.

The Governance/Merge skill, being deterministic, should not be a skill at all: it is
a required status check / CI gate that the worker's identity has no permission to
affect. This is a deletion and an integrity gain simultaneously.

### 2.3 Outcome acceptance is a named, human-owned gate

Per-story gates verify diffs and merges; nothing in v2 verifies that the assembled
project achieves its committed outcome. The dominant failure mode of autonomous
delivery is *locally correct, globally wrong* — every check green, wrong product.

- The planning step writes **falsifiable acceptance criteria** into the Project at
  creation (statements a person can test to a yes/no). "Export works well" is not a
  criterion; "a 50+ page report exports to valid PDF in under 30 seconds" is.
- A human reviews and approves the criteria when the Project goes READY — approving
  a project *is* approving its definition of done.
- After the last story merges, the Project transitions to `awaiting-acceptance` on a
  human queue. The human runs the criteria as tests (minutes, not a code review) and
  records pass/fail per criterion.
- Failure does not close the project: it produces a new story or a planning
  re-invocation, and is logged as an acceptance catch — a first-class metric.
- Acceptance happens **once per project**, not per story. Keep projects small
  (roughly 5–10 stories) so acceptance latency stays within days. For larger
  projects, the planning agent may mark milestone-acceptance checkpoints after
  coherent increments; this is the only legitimate job Sprints were doing.

This gate should stay human longer than any other: it is the cheapest gate per unit
of protection, and the only point where original human intent re-enters the loop.

---

## 3. Component inventory

| Component | Nature | Lifetime | Checked by |
|---|---|---|---|
| CEO / CTO | Human | Always present | — |
| Planning agent | AI judgment | Invoked, exits | Outcome acceptance |
| Sequencer | Deterministic code | Invoked, exits | (deterministic) |
| Worker pool | AI judgment | Invoked per story, exits | Review skill + merge gate |
| Review skill | AI judgment | Invoked per PR, exits | **Human sampling** |
| Merge gate | Deterministic code (CI) | Invoked per merge, exits | (unforgeable by construction) |
| Human sampling | Human | Periodic | — |
| Hazard-class ack | Human | Per hazard diff | — |
| Outcome acceptance | Human | Once per project | — |

Design invariant: **every AI-judgment component has something independent above or
beside it.** Planning is checked by acceptance; workers by review; review by human
sampling. An AI box with only AI boxes around it is the design smell to stop on.

Component notes:

- **Planning agent** merges v2's Product Manager and Chief Architect. Split them only
  when ≥3 concurrent Projects show genuine cross-project coupling — write that
  trigger down now so the split is evidence-driven. Architecture judgment lives as an
  ADR skill plus human review for consequential decisions.
- **Sequencer** replaces the Delivery Manager's mechanical half: reads the declared
  dependency graph, applies WIP limits, marks unblocked stories ready. It is only
  deterministic if dependencies are **explicitly declared at story-authoring time**;
  if order must be inferred, that inference is judgment and needs a check.
- The Delivery Manager's judgment half (decomposition, escalation classification)
  belongs to the planning agent at project start. There is no residue requiring a DM
  role — the cleanest form of the argument that it shouldn't exist.
- **Workers** are v2's one unambiguously correct role-table row: elastic pool, one
  bounded story each, ephemeral. Each attempt carries an attempt counter and a spend
  cap.
- **Review skill** posts findings or approves. Findings return the story to `ready`
  with findings attached; the attempt counter increments.
- **Merge gate** (required CI check): exact-head review evidence, tests green, diff
  scope matches story scope, **test weakening/deletion surfaced as a distinct diff
  class**, no hazard paths touched. All green → auto-merge. Worker identities cannot
  merge, skip, or re-run it.

---

## 4. Communication: shared state, not messages

No component addresses another. The mechanism is **write → transition → route →
invoke → read → write**:

1. A component's last act is a state write to the substrate; then it exits.
2. The change feed (webhook, with a poller as backstop) surfaces the transition.
3. A **dispatcher** — the thin thing the Supervisor becomes — holds a static routing
   table: `transition → responsible role`. Pure lookup, ~10 lines of config, no
   interpretation. It passes only the artifact identity, never content.
4. The invoked component reads everything it needs from the substrate itself.
5. Its write is automatically the next transition. The loop closes.

Properties this buys:

- **Mutual invisibility.** The planning agent doesn't know the sequencer exists.
  The only contract between components is the state schema, so any component can be
  replaced, restarted, or deleted without touching the others.
- **Recovery for free.** A crashed component is re-invoked from the same transition
  and reconstructs identically. A lost wakeup is latency, not an incident — the
  poller sees the still-unhandled state next pass.
- **Humans are just another route target.** `blocked:poison`, `awaiting-ack`, and
  `awaiting-acceptance` route to a human queue through the same table. The only
  difference is SLA; nothing blocks incorrectly meanwhile because blocked state is
  explicit and the sequencer keeps dispatching everything not behind it.

Hard requirements at the sharp edges:

- **Idempotency**, keyed on artifact + state version — the change feed is
  at-least-once.
- **Route on transitions, not states**, and record claims as state; the claim itself
  is the duplicate suppressor. Workers never self-claim — dispatch assigns, which
  removes the label-CAS race by construction.
- **Attempt counter and poison threshold** on every retryable item; poison routes to
  the human queue with full failure context attached.
- Queue items carry routing metadata plus an artifact link only. The moment a queue
  item copies business context, the relay has been rebuilt as infrastructure.
- Any cache over the substrate must be **derivable and disposable** — rebuildable
  from the substrate, never authoritative.

The end-to-end shape: the CTO's direction issue is the pebble; everything after is
chain reaction. It is safe because the brakes are in the chemistry (poison, hazard,
acceptance are transitions *within* the reaction that route to humans) and it
terminates by construction (finite dependency graph, attempt counters, spend caps,
WIP limits). Any loop without a bound is an architecture bug. Other pebbles start
other chains through the same routing table: CI failure → Operations repair; expired
claim → recovery; production incident → triage.

---

## 5. Governance without per-story approval

In the happy path, **zero stories are human-reviewed**. A human sees code in exactly
three conditional situations:

1. **Hazard path touched.** Project-level authorization by default; human
   acknowledgement for any diff touching an enumerated hazard list: secrets and
   credentials, dependency manifests, CI/workflow files, branch protection, IAM,
   data migrations, destructive operations, and **the AI-Team's own policy/governance
   repo** (the self-authorization escape hatch — if agents can write the rules they
   are judged by, governance is decorative). Enforced via CODEOWNERS with agent
   identities excluded. The ack is one approval on one diff, not a story review.
2. **Poison story.** Attempt counter exceeded → `blocked:poison` → human queue with
   all findings attached. Failure-triggered, not default. Rising poison rate is
   itself a signal: badly scoped stories or underpowered workers.
3. **Human sampling of merged PRs.** Not a gate — PRs merge without waiting. It
   answers "is the review skill still trustworthy?", the one question nothing
   downstream of the reviewer can answer. Start high (1-in-3 during customer-zero),
   taper on clean evidence toward 1-in-20, snap back up on findings.

Honest residual: a subtly flawed, plausible-looking PR can reach main unread by any
human. The layered defenses (independent review context, unforgeable gates,
test-weakening detection, outcome acceptance) reduce but do not eliminate this. The
dial is the sampling rate — not a return to per-story gates.

---

## 6. Deletions before implementation

| Delete / collapse | Rationale |
|---|---|
| Chief Architect as a separate agent | Merge into planning agent; split on written evidence trigger |
| Delivery Manager as a role | Mechanical half → sequencer (code); judgment half → planning agent |
| Sprints | Time-boxing and social commitment are human constraints; agents need a dependency-ordered queue + WIP limit. Keep only as acceptance milestones if projects can't be cut smaller |
| Epic layer | Not earning its place at customer-zero scale |
| Governance as an LLM skill | Becomes deterministic CI gate / branch protection |
| Supervisor (until proven needed) | Start with a 60-second cron poll + routing table; add machinery only when polling demonstrably costs too much |
| Heartbeats, liveness, execution-identity management | Downstream of the persistence assumption removed in §2.1 |

Keep as designed: GitHub as system of record; project-bound authority;
skills-over-standing-agents principle; state-derived role queues; Operations
separation (repairs the factory horizontally, never owns product decisions).

---

## 7. Measurement — instrument v1 before migrating

The v2 brief's review criteria are unfalsifiable as written. Baseline these on v1
first, or there is no way to know the migration worked:

- **Human touches per project, classified:** strategic decision / risk acceptance /
  policy exception / defect rescue / infrastructure repair / **pure relay**. Only
  relay should trend to zero; a design that suppresses defect-rescue touches is
  concealing failure, not achieving autonomy.
- Autonomous merge rate; rework rate (reverted/reopened within N days); escaped
  defect rate; acceptance-catch rate (how often "all gates green" was wrong).
- Cost and wall-clock per accepted story; poison rate; stuck-work MTTR.
- Sampling findings rate (review-skill drift detector).

---

## 8. Minimal customer-zero proof

Compress v2's Stages 1–3 (document-writing is the least falsifiable artifact) and run:

- One real project, 5–8 stories with explicitly declared dependencies, falsifiable
  acceptance criteria approved by a human at READY.
- 2–3 concurrent workers. Sequencer as code. Review on PR-open transitions. Merge
  gate deterministic in CI. Hazard list enforced via CODEOWNERS.
- **No Supervisor** — a 60-second cron poll and a routing table.

Kill criteria, stated in advance: relay touches above X; stories needing human
rescue above Y%; cost per accepted story above Z; any semantic merge failure the
gates passed → RETHINK before scaling.

---

## 9. Worked example — "Users can export reports as PDF"

**Happy path.** (1) CTO writes a direction issue — the last routine human touch until
acceptance. (2) Planning agent, invoked on `ready-for-planning`, reads direction +
ADRs from the substrate, writes: a Project with falsifiable criteria (valid PDF for
50+ page reports in <30s; export respects permissions), stories S1 rendering service
← nothing, S2 endpoint ← S1, S3 UI ← S2, S4 permissions ← S2; exits. Ambiguity would
instead route a clarification to the CTO queue. (3) Sequencer marks S1 ready.
(4) A worker is dispatched (never self-claims), implements S1, opens a PR, exits.
(5) Review fires on PR-open in fresh context: findings return the story to ready
(attempt 2 reads findings + spec); approval labels the PR. (6) Merge gate (CI) checks
evidence, tests, scope, test-integrity, hazard paths → auto-merge. (7) Merge
transition → sequencer unblocks S2 and S4 in parallel; loop repeats per story.
(8) Graph empty → `awaiting-acceptance` → human tests the criteria in minutes;
pass closes the project, fail produces a story or planning re-invocation.

**Exception paths.** S2 adds `pdf-lib` → manifest is a hazard path → one human ack
on that diff, then merge. S4 fails review three times → `blocked:poison` → human
queue with all findings — the logged "defect rescue" touch that keeps metrics honest.
Weekly sampling audits ~1 in 10 merged PRs for reviewer drift.

The CTO appeared at: one direction issue, one dependency ack, one acceptance test.
Every v1 appearance between steps 2→3, 3→4, or 7→2 is a relay touch this design
deletes — and that diff is the migration's success metric.

---

## 10. What the CTO's job becomes

Not a link in the chain: the person who **chooses which pebbles to drop and audits
the reaction traces afterward**. Falsifiable acceptance criteria are pre-commitment
to what a successful reaction looks like; touch metrics measure whether the chain ran
clean. Human attention concentrates where the design routes it: product strategy,
risk acceptance, hazard acks, poison rescues, sampling, and final outcome judgment.
