# Stabilization architecture review (2026-09-03)

Independent review testing the 13-part stabilization proposal in PR #645
(`factory/learning/phase5-stabilization-architecture-review-prompt-2026-09-03.md`)
against the evidence in `factory/learning/phase5-rung2-independent-rca-2026-09-03.md`
and against the current code — `factory/dispatcher/dispatcher.py`,
`factory/runtime/workers.py`, `factory/runtime/operating_envelope.py`,
`factory/runtime/production_readiness.py`, `factory/acceptance/e2e_doctor.py`,
and `factory/spec/state-schema.md` §4.3. Neither the RCA's framing nor the
proposal's is assumed correct going in.

## Verdict

The proposal is aimed at the right six problems. It is not the smallest
architecture that solves them — about a third of it already exists in the
code or spec, and the part that's genuinely new is oversized relative to the
evidence for three of the six root causes.

Two items — moving delivery off the Mac (11) and freezing the factory during
qualification (13) — are the highest-leverage changes available and should
ship before anything else. One item — Planning validating against the
repository (2) — closes a real, twice-repeated gap (#76, #100). One item — a
hard rule that Planning cannot relabel a merged story (12) — fixes an
incident that already happened and costs almost nothing to build. The rest of
the proposal (a second full independent-review pass, a formal five-way defect
taxonomy, a Story-complexity gate, restating what Planning and Delivery
already own) is heavier machinery than five runs of evidence justify, and one
item claims territory — attempt accounting — that the spec already covers
correctly; the actual bug there is much smaller than "build a classifier."

## A — Coverage of the RCA

| RCA finding | Covered by | Coverage |
|---|---|---|
| RC-1 — unverifiable Chrome/console/timing criteria (5 of 8 projects) | 1, 6, 7 | Covered if "NOT VERIFIED blocks completion" is built and enforced, not just declared |
| RC-2 — unstable Mac delivery substrate (0/7 Anthropic launches completed on Story #107; $17.88 of $31.52 wasted on #18) | 11 | Covered — the only item that names moving off the Mac explicitly |
| RC-3 — infrastructure failures consuming Attempts | 9, 10 | Partially covered — the spec rule already exists; the bug is narrower than proposed |
| RC-4 — factory changed continuously during qualification (146 commits, 4 PRs mid-run on #100); doctor grew 11 defects of its own | 13; nothing addresses doctor complexity | Partially covered — freeze is right; doctor bloat isn't addressed anywhere in the 13 items |
| RC-5 — Planning doesn't validate scope/dependencies against the repo (#76 S80, #100 S104, #47, #30) | 2 | Covered directly |
| RC-6 — frozen run bundles missing worker outcomes / review verdicts / untruncated errors (#18, #60, #76) | none | Not covered — missing control |

Four of six causes are addressed; one is only partly addressed by the
proposed mechanism; one — evidence completeness — has no proposed control at
all.

## Answers to the review tasks

### B — Is Planning the right owner for FRs, NFRs, ADRs, decomposition, and required executor?

Yes for validation (item 2) and verifiability (item 1's "must not create an
untestable requirement"). No objection to the boundary. The concern is scope
creep inside item 1: "FRs, NFRs, ADRs, Story decomposition, acceptance
criteria, required test cases for every FR/NFR, positive/negative/boundary
cases, verification type" is a full requirements-engineering document format.
Planning already produces Story specs, ADRs, and — since the pre-Rung-3
changes — an operating envelope (`factory/runtime/operating_envelope.py`).
Layering a second, parallel requirements taxonomy (FR-/NFR-numbered, with its
own ID scheme) on top of the envelope's own `OE-` IDs is two ID systems for
one job. Extend the envelope's schema to carry "which runner verifies this"
per entry rather than inventing a second document.

### C — Should the Independent Reviewer validate both the plan and the delivered code?

The reviewer currently only runs once, on PR-open
(`factory/agents/review/invoke.py`) — there is no plan-review stage today;
item 4 would be entirely new. The conflict risk is real: the same reviewer
approving a plan and then approving the code built to that plan is grading
its own homework on the second pass — findings on "does this satisfy the
plan" collapse to "does this match what I already blessed." It also doubles
reviewer engine cost and adds a full round-trip to every project's cycle
time, which is already unmeasured or 14+ hours in three of five runs. Cheaper
alternative that gets most of the value: make item 2 (repo validation) a
deterministic, non-LLM check — file existence, dependency conflicts, test
contradictions are all mechanically checkable — and skip a second AI review
pass. Reserve full independent plan review for later if the deterministic
check proves insufficient.

### D — Would 100% requirements-to-test coverage reduce observed Review to Delivery loops?

Yes, for exactly the incidents that already happened. Story #93 (#89) spent
16 attempts because "real Chrome responsiveness and console-error assurance"
had no available runner and nothing stopped Delivery from retrying against an
unprovable requirement. Story #107 (#100) spent 9 launches partly for the
same reason. If "NOT VERIFIED blocks completion" routes back to
Planning/human instead of back to Delivery, both of those loops end in 1
attempt instead of 16 and 9. That is the single highest-value mechanical
change in the whole proposal after moving delivery off the Mac.

### E — How many attempts/recoveries could reclassification have avoided?

Fewer than the proposal implies, because the "no reclassification exists
today" premise is wrong. `factory/spec/state-schema.md` §4.3.4 already states
infrastructure failures don't count, and `factory/dispatcher/dispatcher.py`
already has two release paths: `release_unstarted_failure` (restores
Attempt, "the reserved worker did not start") and `release_definite_failure`
(preserves Attempt, "a worker really ran and failed"). What's missing is
narrower: the capacity-pool router already computes a finer signal per launch
— `mutation_state` (`none` / `post-mutation`) and `terminal_outcome`
(`started-mid-work-failed` vs a true no-start) — but on Story #107 three of
the seven failed launches carried `mutation_state: post-mutation` while the
poller's own durable message on the same event still read "a definite
failure proves the worker did not start." That is a reconciliation bug
between two systems that already agree on the concept, not a missing
concept. Fixing that one seam — route the capacity-pool's already-computed
mutation signal into the existing two-path choice — would correctly
re-classify those events without building item 9's five-category taxonomy
from scratch. Of the 54 attempts across five runs, the 15-plus
infrastructure-attributed ones in RC-2 are the direct candidates; the
reconciliation fix, not a new taxonomy, is what closes most of that gap.

### F — Runtime environment: smallest evidence-backed choice?

Move Delivery to GitHub Actions or a dedicated Linux runner for the routes
that need a browser, and keep the Mac only for routes that don't. This isn't
a judgment call — it's already proven inside the data: on Story #107, branded
Chrome crashed under the Codex app on the Mac and the identical headless
Chrome channel ran clean in GitHub Actions the same day. A hybrid (Mac for
text-only delivery, CI runner for anything touching a browser) is the
smallest change that fixes the observed failure without a wholesale platform
migration.

### G — Doctor complexity: what should move or go?

The doctor is 656 lines with three modes (`rehearsal`, `preplanned`,
`resume`) and roughly twenty check methods. Eleven of the 53 logged Phase 5
findings are defects in the doctor itself, including five in a row (P5-044
through P5-049) where each fix was blocked by the next doctor defect. None of
the proposal's 13 items mention this. Repository/dependency/scope validation
(item 2) belongs in Planning, at plan-approval time, not in a pre-flight
check run once per attempt — move it out of the doctor entirely. Engine-route
health belongs in Runtime (item 11's recovery/failover), continuously, not as
a one-shot gate — move it out too. What's left in the doctor after that is
genuinely doctor-shaped: worktree creation, credential presence, merge-gate
configuration, competing-poller detection. That's roughly a third of its
current size.

### H — Measurement: separate metrics per component

See the metrics list below. The core fix: report autonomy twice (engineering
vs. operational), and never let a Runtime failure count against Delivery's
number — which requires the E reconciliation fix to be real, not just
declared.

## Disagreements with the proposal

**Item 9/10 overstate the gap.** The proposal frames attempt accounting and
defect classification as though nothing exists. The spec rule (§4.3.4) and
two dispatcher functions (`release_unstarted_failure`,
`release_definite_failure`) already implement the binary version correctly.
Building a five-category taxonomy (`DELIVERY_DEFECT` / `PLANNING_DEFECT` /
`RUNTIME_FAILURE` / `HUMAN_DECISION_REQUIRED` / `UNKNOWN`) before fixing the
one confirmed reconciliation bug between the capacity-pool's mutation signal
and the dispatcher's binary choice is solving an undemonstrated problem
before the demonstrated one.

**Item 4's cost isn't priced.** A second full independent-review pass before
every plan approval, on top of the existing post-PR review, roughly doubles
reviewer engine spend and adds a full round-trip per project. None of the
five runs recorded a defect that only a full LLM plan-review would have
caught and a deterministic repo-validation check (item 2) would have missed.
Build the cheap version first; add the expensive version only if it proves
necessary.

**Items 1, 5, and 7 mostly restate current ownership.** Delivery already
implements Stories and runs tests; Planning already authors specs and ADRs.
Codifying this as a formal contract is fine hygiene but isn't what fixed or
would have fixed any of the five failed runs — none of the sixteen stories
failed because a component did work outside its lane.

**Item 13 stops one step early.** "Freeze, then run one fresh Rung 2" skips
proving the chosen delivery environment is actually reliable first. Nine
launches for one story (#107) with a 0-of-7 completion rate on one route is
not a sample size anyone should qualify against. Insert a cheap substrate
check — twenty trivial, identical stories through the frozen factory,
measuring launch success per engine route — before spending a real
qualification run on it.

## Missing controls

- **Evidence-completeness gate for run bundles (RC-6).** Nothing in the 13
  items requires a frozen bundle to contain a worker outcome, a review
  verdict, and the untruncated terminal error for every attempt. Three of
  five past bundles (#18, #60, #76) don't. Add: a bundle missing any of these
  is INCONCLUSIVE, not FAIL and not PASS — the run doesn't count either way.
- **Reviewer sibling-story context.**
  `factory/learning/project67-story70-review-retrospective.md` documents the
  reviewer charging Story #70 for work that Story #71 owns, because
  `factory/agents/review/invoke.py::build_input` supplies the whole-project
  checklist but not story dependency ownership. Item 8 lists what the
  reviewer should check but not this specific input gap that already caused
  a poisoning.
- **Doctor complexity reduction.** Discussed under G — no item proposes
  shrinking the doctor even though it's the single most defect-prone
  component in the issue log (11 of 53 findings).
- **Cost-completeness.** Four of five runs report cost only as a lower bound
  (subscription-backed engines report no per-invocation price; several
  Anthropic failures on Story #107 reported no usage at all). Not blocking,
  but should be an explicit KPI-availability field, not silently absent.

## Minimum stabilization plan

| Item | Priority | Tied to |
|---|---|---|
| Move browser-touching delivery to a Linux/CI runner; keep the Mac for the rest (11) | MUST | 0/7 Anthropic launches, #107; $17.88/$31.52 wasted, #18 |
| Planning validates scope files, dependency conflicts, and existing-test contradictions before approval (2) | MUST | #76 S80 (missing app.js); #100 S104 (Playwright vs. existing test) |
| "No verification runner named -> NOT VERIFIED -> blocks completion, routes to Planning/human, not another Delivery attempt" (6+7, merged) | MUST | #89 S93 (16 attempts); #100 S107 (9 launches); #60/#76 (human-in-Chrome scored as recovery) |
| Planning cannot relabel or reopen a merged Story (12) | MUST | #100 S102/S103 — operator had to restore lifecycle state after a re-plan touched merged stories |
| Qualification freeze: tag, no factory merges during the run (13) | MUST | 146 commits / 48 PRs across five runs; 4 factory PRs merged during #100 |
| Reconcile capacity-pool mutation signal into the existing two-path Attempt release (targeted fix, not full item 9/10) | MUST | #100 S107 — 3 of 7 failed launches showed post-mutation while classified as "did not start" |
| Evidence-completeness gate on frozen bundles (missing control, not in proposal) | MUST | #18/#60/#76 bundles hold only claim events |
| Substrate pre-check: 20 trivial stories, >=95% first-attempt completion, before the next real Rung 2 (extends item 13) | MUST | 0-of-7 completion rate on one route is not an interpretable sample |
| Deterministic (non-LLM) repo-validation check, not a second full independent plan review (lighter version of 4) | SHOULD | same evidence as item 2; cheaper mechanism |
| Formalize the five-way classification taxonomy and routing (9) | SHOULD | good hygiene once the MUST reconciliation fix lands; not blocking |
| Story complexity/decomposition gate (3) | SHOULD | weak direct evidence; human plan-approval can catch this manually one more cycle |
| Reviewer sibling-story context fix (missing control) | SHOULD | #67 S70/S71 — one incident, known root cause, cheap fix |
| Full independent Plan Review as a second LLM pass (4, heavy version) | DEFER | no incident required it specifically over the deterministic check; doubles reviewer cost |
| Formal FR/NFR/ADR contract document layered on top of the existing envelope (1, as written) | DEFER | extend the envelope's own schema instead of adding a parallel one |
| Restating Delivery/Planning ownership boundaries (5, 1's non-validation parts) | DEFER | no run failed because a component worked outside its lane |
| Adversarial self-review / falsification probes (5, 8) | DEFER | no run showed Delivery gaming tests or Review missing a gamed test |
| Doctor complexity reduction (missing control) | DEFER for now, track | 11 of 53 findings are doctor defects, but shrinking it isn't blocking the next run |

## Proposed next-qualification metrics

- **Engineering autonomy** — stories merged with zero Delivery-attributable
  rescue, divided by stories merged. Isolates code quality from everything
  else.
- **Operational autonomy** — stories merged with zero human touch of any
  kind, divided by stories attempted. The number the 75% bar should actually
  apply to.
- **Launch success rate, per engine route** — measured continuously by
  Runtime, not just at doctor time. This is what would have shown the Mac's
  Anthropic route failing before spending a real Story on it.
- **Planning-catch rate** — scope/dependency conflicts caught by the
  repo-validation check before Delivery, versus caught after (by a poisoned
  attempt). Want the second number at zero.
- **Evidence completeness** — percentage of runs whose bundle has a worker
  outcome, a review verdict, and an untruncated terminal error for every
  attempt. Must be 100% for a run to count toward any other KPI.
- **Cost completeness** — percentage of engine invocations with reported
  usage. Report separately from cost itself; a low number here means the
  dollar figure is a guess, not a measurement.

## Phase 5 stabilization finish line

Stabilization is complete, and the factory is ready to unfreeze for a real
Rung 2, when all six hold:

1. The substrate pre-check (twenty trivial stories on the frozen factory)
   reaches 95% or better first-attempt completion on every engine route
   still in the pool; any route below that is removed or moved off the Mac.
2. Zero acceptance criteria in the three most recently approved Projects
   lack a named, existing verification runner.
3. Zero Delivery attempts in the substrate check are attributable to a
   scope or dependency conflict Planning could have caught by reading the
   repository.
4. Planning cannot relabel or reopen a merged Story — enforced by a test
   that fails the build if the code path exists, not by a written rule.
5. The qualification-freeze procedure (tag, no factory merges for the run's
   duration) is written down as a runbook and was actually followed for the
   substrate check.
6. A fresh, frozen, evidence-complete Rung 2 of comparable difficulty to the
   failed one passes: operational autonomy >= 75%, relay = 0, evidence
   completeness = 100%.

Only after all six does Rung 3 planning start. Any one missing means
stabilization isn't done, whatever the individual run's autonomy number
says.
