# Phase 5 Rung 2 retrospective

**Verdict: FAIL with useful findings.** The product outcome for the original
Project #18 was accepted, but the factory missed its autonomy threshold. Later
attempts to obtain a qualifying repeat exposed a live-data defect, a
realistic-scale performance defect, and runtime controls that can turn lack of
capacity into poisoned work. Rung 3 must not start until the bounded changes in
`factory/spec/phase5-pre-rung3-improvement-plan.md` are implemented and a fresh,
independent Rung 2 project passes.

## What was measured

The checked-in reproducible report covers Product Project #18 and Stories
#20–#23 only. It reports:

- autonomy: 2 of 4 Stories, or 50%, against a minimum of 75%;
- relay: 0, which passed;
- escaped defects: 0, which passed for that accepted project;
- 11 worker attempts, including 7 retries;
- 2 of 4 Stories poisoned; and
- complete measurement integrity.

The frozen source is `runs/rung2/final/evidence.json`. The machine-readable
result is `runs/rung2/final/report.json`, and the plain-language rendering is
`runs/rung2/final/report.md`.

Project #30 and Project #47 were later qualifying-repeat candidates. They do
not belong in Project #18's KPI denominator. They also do not have a completed,
combined Rung 2 KPI bundle. Their findings are evidence for improvement, not a
retroactive rewrite of the Project #18 result.

## Findings

Every finding uses one of the required classifications: `product-specific`,
`factory-systemic`, or `deferred-with-reason`.

### F1 — realistic portfolio size froze Project #47

- **Observation:** toy values such as $10 and $20 passed, but a $1,000,000
  portfolio caused more than 100 million cent-level simulations and left the
  browser renderer busy for minutes.
- **Evidence:** [Project #47](https://github.com/maheshhbhat/income-portfolio-analyzer/issues/47)
  and [Story #51's owner verification](https://github.com/maheshhbhat/income-portfolio-analyzer/issues/51#issuecomment-5424581733).
- **Root cause:** planning specified exact cent-by-cent search without an
  operating envelope; delivery implemented it literally; review exercised
  correctness on tiny fixtures; the project had no independent readiness check
  at representative scale.
- **Classification:** `factory-systemic`. The slow algorithm is product code,
  but four factory stages all permitted an unbounded design to reach the owner.
- **General lesson:** passing examples do not establish production usability.
  A performance-sensitive requirement needs explicit scale and response-time
  bounds that every later stage consumes.
- **Factory change:** introduce one project-level operating-envelope artifact,
  require delivery feasibility evidence, make review exercise the same envelope,
  and add a post-merge production-readiness evaluation.
- **Regression/evaluation proof:** a Project #47-class fixture with a $1,000,000
  portfolio must complete within an approved browser interaction budget and
  remain responsive. Tiny examples remain correctness tests, not readiness
  evidence.
- **Rung 3 validation:** the fresh Rung 2 repeat must include representative
  minimum, normal, and upper-bound inputs before acceptance is offered.

### F2 — Project #30 passed static behavior but failed live provider behavior

- **Observation:** Vanguard dynamic yield retrieval remained wrong when checked
  against the live provider, despite narrower automated evidence passing.
- **Evidence:** [Project #30](https://github.com/maheshhbhat/income-portfolio-analyzer/issues/30).
- **Root cause:** the plan and review contract did not distinguish deterministic
  parser tests from a bounded live-provider contract check. Production readiness
  was inferred from fixtures.
- **Classification:** `factory-systemic`. The provider adapter needs a product
  fix, but accepting fixture evidence as proof of live integration is a reusable
  factory error.
- **General lesson:** fixture correctness and live dependency compatibility are
  different claims and need different evidence.
- **Factory change:** the operating envelope declares external dependencies and
  freshness requirements; readiness runs a bounded, read-only live check when
  the project makes a live-data claim.
- **Regression/evaluation proof:** a Project #30-class evaluation must detect a
  provider response change or stale fallback while deterministic offline tests
  remain reproducible.
- **Rung 3 validation:** any Rung 2 repeat using a live provider must preserve
  both offline parser proof and a timestamped live-contract result.

### F3 — the poller could start without a successful doctor result

- **Observation:** the repeat was started without running the readiness doctor.
- **Evidence:** the operator sequence recorded in [factory issue #541](https://github.com/maheshhbhat/ai-software-factory/issues/541#issuecomment-5426792486).
- **Root cause:** doctor was documented as a prerequisite but the mutable poller
  did not require proof that it had passed for the same repository, commitment,
  factory revision, and configuration.
- **Classification:** `factory-systemic`.
- **General lesson:** a safety check that can be skipped is advice, not a gate.
- **Factory change:** doctor emits a short-lived, scoped readiness receipt and
  every mutable poller entrypoint validates it before any GitHub mutation.
- **Regression/evaluation proof:** missing, failed, expired, wrong-repository,
  wrong-commitment, wrong-revision, and wrong-configuration receipts all block;
  a matching receipt permits a dry and live startup.
- **Rung 3 validation:** the fresh Rung 2 evidence must include the accepted
  doctor receipt identifier and its validated scope.

### F4 — two pollers could run for the same queue

- **Observation:** two pollers ran concurrently for the same repository and
  commitment.
- **Evidence:** the duplicate-process observation recorded in
  [factory issue #541](https://github.com/maheshhbhat/ai-software-factory/issues/541#issuecomment-5426792486).
- **Root cause:** Story claims prevent most duplicate worker dispatch, but there
  was no process-level singleton at the poller boundary. Reconcilers could race
  and duplicate external work or confusing logs before a Story claim settled.
- **Classification:** `factory-systemic`.
- **General lesson:** item-level idempotency does not replace single ownership of
  a mutable queue loop.
- **Factory change:** acquire an operating-system lock keyed by canonical
  repository plus commitment before doctor validation or mutation. A duplicate
  exits with a stable, nonzero status. A crash releases the lock.
- **Regression/evaluation proof:** a second same-key poller is refused; a
  different commitment can run; abrupt termination releases the same-key lock.
- **Rung 3 validation:** startup evidence must name the acquired poller key and
  prove there was one owner for the run.

### F5 — lack of model capacity consumed Story #58's recovery budget

- **Observation:** `no-eligible-capacity` launches repeatedly claimed Story #58
  and it reached `story:blocked:poison` even though no model accepted useful
  work.
- **Evidence:** [Story #58](https://github.com/maheshhbhat/income-portfolio-analyzer/issues/58)
  and the state rule that infrastructure failures do not count in
  `factory/spec/state-schema.md`.
- **Root cause:** eligibility and capacity were resolved after durable claim
  accounting. Recovery bounded the loop, but the system treated admission
  failure like an ambiguous worker failure.
- **Classification:** `factory-systemic`.
- **General lesson:** admission is not an attempt. The retry budget should begin
  only after a worker is reserved and its invocation actually starts.
- **Factory change:** reserve an eligible route before the Story claim; bind the
  reservation to that dispatch; record worker-start evidence; release an unused
  reservation without consuming an Attempt.
- **Regression/evaluation proof:** repeated zero-capacity polls leave the Story
  ready with the same Attempt value and no recovery-count growth; capacity
  restoration permits one claim and one start; an expired reservation fails
  closed without poisoning.
- **Rung 3 validation:** the fresh Rung 2 run must include a controlled capacity
  loss and recovery before product delivery, with no human rescue.

### F6 — the real model-adapter boundary needs production-shaped proof

- **Observation:** planning and delivery failures repeatedly appeared only at
  real engine boundaries: authentication, structured stream shape, capacity,
  permissions, effort, and terminal outcome interpretation.
- **Evidence:** findings P5-018, P5-025, P5-035–P5-037, P5-042, and P5-043 in
  `factory/spec/phase5-issue-log.md`.
- **Root cause:** fakes established internal behavior but did not exercise the
  exact adapter protocol used by the selected real model.
- **Classification:** `factory-systemic`.
- **General lesson:** every configured route needs a cheap real contract probe;
  passing a fake for one engine says nothing about another engine's stream,
  identity, permissions, or capacity semantics.
- **Factory change:** extend doctor with bounded, read-only probes for every
  route that may be selected, including fallback routes, and normalize results
  into `eligible`, `temporarily unavailable`, or `contract failure`.
- **Regression/evaluation proof:** recorded production-shaped streams cover a
  valid result, malformed result, rate limit, session capacity, authentication
  failure, and fallback selection. A live probe verifies the currently enabled
  adapter without changing a product repository.
- **Rung 3 validation:** the selected primary and fallback routes must both have
  current doctor evidence before the repeat begins.

### F7 — the original KPI report does not cover later repeat attempts

- **Observation:** Project #18 has a deterministic report; later Project #30
  and #47 work has issue evidence but no completed combined repeat report.
- **Evidence:** `runs/rung2/final/report.json` identifies Project #18 explicitly.
- **Root cause:** the reporter is tied to a preserved completed run, while later
  candidate runs were interrupted before a new bounded evidence bundle closed.
- **Classification:** `deferred-with-reason`.
- **General lesson:** do not merge partial campaigns into a finished KPI result.
- **Factory change:** no new reporting platform. Reuse the existing generator
  for the fresh repeat and require its frozen bundle before declaring a verdict.
- **Regression/evaluation proof:** regeneration from the fresh bundle must be
  deterministic and name exactly one Project plus its 2–4 Stories.
- **Rung 3 validation:** Rung 3 remains blocked until that fresh report exists
  and passes all three Rung 2 thresholds.

### F8 — cost per accepted Story was unavailable

- **Observation:** Project #18 recorded a known reported-cost lower bound, but
  one Claude invocation and the Codex invocation had no reported price. Cost per
  accepted Story was therefore unavailable.
- **Evidence:** `runs/rung2/final/report.json` records 15 invocations, 2 without
  cost, and a known reported total of $31.523321.
- **Root cause:** the adapter contract did not require every terminal invocation
  to emit either an exact cost or an explicit reason and independently
  reproducible usage fields.
- **Classification:** `factory-systemic`.
- **General lesson:** a lower bound is honest, but it cannot support the Rung 3
  cost kill criterion.
- **Factory change:** every adapter emits a terminal usage receipt. Exact vendor
  cost is preferred; otherwise token/tool usage and a named unavailable reason
  are mandatory. The reporter remains fail-closed and never converts a lower
  bound into an exact cost.
- **Regression/evaluation proof:** mixed priced and unpriced invocations keep
  cost unavailable; complete receipts deterministically reproduce cost per
  accepted Story; a missing receipt fails measurement integrity.
- **Rung 3 validation:** the fresh Rung 2 repeat must report exact cost per
  accepted Story or fail measurement integrity before Rung 3.

### F9 — retries and worker outcomes were not diagnostically separated

- **Observation:** Project #18 used 11 attempts for 4 Stories. During the wider
  run, a worker stopped by its spend limit was described as if it never started,
  and later capacity refusal was treated like a completed worker failure.
- **Evidence:** Project #18 attempt details in `runs/rung2/final/report.json`,
  findings P5-035–P5-037 in `factory/spec/phase5-issue-log.md`, and Story #58's
  `no-eligible-capacity` recovery comments.
- **Root cause:** terminal outcomes collapsed admission failure, launch failure,
  mid-work failure, limit termination, and product/test failure into broad
  failure paths. Retry counts could be measured but not reliably attributed.
- **Classification:** `factory-systemic`.
- **General lesson:** lowering retries starts with naming where work stopped. A
  retry policy cannot improve from an ambiguous failure label.
- **Factory change:** define mutually exclusive durable stages and reason codes:
  `not-admitted`, `launch-failed`, `started-mid-work-failed`, `limit-stopped`,
  `validation-failed`, and `completed`. Attempt accounting keys off the durable
  worker-start boundary.
- **Regression/evaluation proof:** production-shaped cases prove each outcome is
  classified once, reports attribute retries by reason, and capacity/launch
  failures do not consume a product attempt.
- **Rung 3 validation:** the fresh Rung 2 report must attribute every retry and
  show whether factory changes reduced the Project #18 baseline of 7 retries.

## Product-specific work kept out of this factory change

The Project #47 search algorithm and the Project #30 Vanguard adapter still need
product corrections. They are useful regression subjects, but this plan does
not implement them, rescue Story #58, or alter their lifecycle state.
