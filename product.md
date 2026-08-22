# AI Software Factory — Product Definition

## Purpose

The AI Software Factory turns an approved roadmap commitment into reviewed,
tested, auditable software changes with as little routine human coordination as
possible.

The product is successful when a human chooses the outcome and the safety
limits, while the factory plans, sequences, implements, reviews, verifies, and
records the work through repeatable rails.

## Primary user

The primary user is the repository owner acting as product and risk owner. They
should be able to:

- state a roadmap commitment;
- approve or reject a bounded project plan;
- decide only the named risk, rescue, sampling, cutover, and acceptance bells;
- understand plans, evidence, failures, costs, and residual risks in plain
  language;
- reconstruct what happened from GitHub and the canonical logs without relying
  on an agent's memory or private reasoning.

Maintainers and auditors are secondary users. They need deterministic evidence,
replay-safe state, and explicit failure reasons.

## Product outcomes

1. A commitment becomes a finite dependency-ordered project with falsifiable
   acceptance criteria.
2. Eligible stories move through claim, implementation, fresh-context review,
   deterministic merge checks, and completion without manual relay work.
3. Every human decision is visible as an authoritative GitHub artifact and one
   canonical touch-log receipt.
4. Failures stop safely, name what failed, and route only genuine judgment back
   to the owner.
5. Each accepted increment reports enough evidence to measure autonomy, retries,
   poison, escaped defects, acceptance catches, cost, cycle time, and human
   touches.

## Standing constraints

- GitHub issues, labels, comments, pull requests, checks, and history are the
  authoritative delivery state. Caches and working copies are disposable views.
- Read repository state from a freshly fetched `origin/main`, not from an
  assumed-current working tree.
- AI workers are invoked for bounded work, write durable artifacts, and exit.
  They do not become persistent coordinators or sources of state.
- Phase work and new capabilities are bounded projects under a roadmap commitment
  and end at an explicit outcome-acceptance decision. A standing maintenance
  project may exist only through an accepted architecture change that states its
  narrow scope, missing acceptance bell, and compensating controls; it must never
  absorb Phase work or new capability.
- Story dependencies are explicit and acyclic. Repository-wide work in progress
  is capped at two claimed stories.
- A story receives at most three dispatched attempts before poison handling.
  Recovery and human rescue are finite.
- Factory-authored stories default to a worker spend cap of exactly `$5 / 60
  min` per invocation unless an approved plan states another bounded value.
- Product stories live with the product code. Factory stories live in this
  repository. Cross-repository ownership and merge protection must be explicit;
  never pretend a gate exists in another repository.
- Private-repository read access is a precondition. Missing access fails loudly
  before any partial planning or delivery write.
- Hazard paths require the named human acknowledgement. Workers cannot weaken
  rules, required checks, branch protection, or their own evaluation boundary.
- The worker never merges or approves its own work. Review runs in fresh context
  from the diff, story, project criteria, and relevant ADRs. Deterministic checks
  decide merge eligibility.
- Shared GitHub credentials are a temporary, documented limitation—not an
  independent identity boundary. Do not describe them as one.
- Every requirement maps to a named automated test. Wiring claims need controlled
  end-to-end evidence against real integrations, or an owner-approved bounded
  limitation with substitute evidence and residual risk.
- Coverage is measured reproducibly and reported before acceptance. It is never
  enforced as a percentage threshold.
- Human-facing plans and reviews use short, plain language. Planning digests
  include a system-flow diagram, a dependency diagram, and text alternatives.
- Phase work advances only after the preceding bounded project is accepted.
  Findings become corrective work and are verified before the next phase or rung.

## Human decision policy

Routine forwarding is not a human decision. A person participates only for:

- plan approval;
- hazard acknowledgement;
- poison rescue;
- scope decisions;
- sampling audit;
- cutover approval;
- outcome acceptance.

Every such bell is recorded once. Pure relay touches must trend to zero. Decision,
audit, and rescue touches are evidence of governance and must not be hidden to
make autonomy appear higher.

## Quality bars

- **Correctness:** Approved acceptance criteria are falsifiable and backed by
  evidence that can fail.
- **Safety:** Missing, stale, ambiguous, malformed, untrusted, or unverifiable
  input fails closed before an authoritative state change.
- **Determinism:** The same authoritative input produces the same eligibility,
  identity, dependency, gate, and replay result.
- **Auditability:** A reviewer can identify the artifact, exact commit, test,
  transition, timestamp, and human decision supporting each material claim.
- **Idempotency:** Replaying an event or decision creates no duplicate issue,
  transition, pull request, or touch receipt.
- **Boundedness:** Work, attempts, recovery, spend, waits, and human exceptions
  have explicit limits and terminal outcomes.
- **Test integrity:** Tests cover positive and negative paths. Deleting or
  weakening tests is surfaced rather than hidden inside a green result.
- **Observability:** A stopped factory is distinguishable from an idle factory,
  and every refusal states a readable reason and an existing next action.
- **Accessibility:** Diagrams supplement rather than replace a plain-language
  explanation.

## Phase 5 measurement policy

Every rung uses the same delivery loop and writes a reproducible report under
`runs/`. The report includes:

- touches by classification, with relay reported explicitly;
- autonomous merges divided by total stories;
- dispatched worker attempts divided by stories;
- poisoned stories divided by stories;
- post-merge escaped defects;
- acceptance catches;
- actual cost per accepted story;
- cycle time from project plan approval to outcome acceptance.

Rung 1 proves the measurement and delivery plumbing on the toy `/health`
scenario. Rung 2 applies the same loop to a genuine small product feature. Rung
3 applies it to the notifications-extraction strangler with shadow and cutover
controls.

The fixed kill criteria remain: relay above zero, rescues above 30% of stories,
any comparator-visible defect reaching cutover, or cost per accepted story above
the owner-approved ceiling stops further use and requires a rethink. The monetary
cost-per-story ceiling is intentionally unresolved here; the owner must set it
before approving the Rung 3 plan. Do not invent it during planning.

## Non-goals

- Replacing product strategy, risk ownership, or final acceptance with model
  judgment.
- Adding a supervisor, event bus, epic layer, skills library, multi-project
  scheduler, or progress dashboard before Phase 5 evidence earns it.
- Hiding rescue, risk, sampling, or acceptance work to improve an autonomy metric.
- Treating line coverage, model confidence, a summary, or another agent's claim as
  proof of correctness.
- Weakening safety controls to reduce cycle time or cost.
- Using the factory repository as the home for product-code stories.

## Planning guidance

Plan the smallest bounded increment that produces outcome evidence. Prefer
roughly 5–10 stories, but do not manufacture stories to hit a count. Keep each
Phase 5 rung in its own project. After each rung, record findings, revise affected
prompts or plans through the rails, re-verify what changed, and obtain acceptance
before planning the next rung.
