# Project #64 operational-learning record — Career Copilot contextual follow-ups

Career Copilot Project #64 (`maheshhbhat/career-intelligence-mcp`), delivering
issue #37 ("Add contextual suggested follow-ups as clickable chat prompts")
through the AI Software Factory's repository-native Planning path, as a real
product delivery run doubling as operational-learning evidence for the
Factory itself. This record covers the portion of the run with direct,
checked evidence available to the author at time of writing; where earlier
Planning attempts occurred without evidence available here, that is stated
explicitly rather than estimated.

## Final product outcome

Delivered and merged. Three Stories, in dependency order:

| Story | What it delivered | PR | Merge commit | Merged at (UTC) |
|---|---|---|---|---|
| #67 | Deterministic follow-up derivation, shared by both chat backends | #70 | `a2bdbf443e17b0da972a40384d648ac49e30c57b` | 2026-09-21T12:19:21Z |
| #68 | Follow-up chips wired into the hosted chat + favicon handling | #73 | `85355bcc84f73af1ce622fe845fbe6ea29a1b944` | 2026-09-21T14:23:41Z |
| #69 | Headless-browser E2E assurance for the chips | #75 | `8920418df7c518c3b3745ce8bfda479cf01eb97a` | 2026-09-21T16:53:57Z |

Final state of `master`: `8920418df7c518c3b3745ce8bfda479cf01eb97a`. Real
GitHub Actions CI ("test" check) is green on that exact commit, confirmed
directly against the check-run API, not inferred from a PR's own status.

Project #64 reached `project:awaiting-acceptance` at the point of writing.
Acceptance itself is a separate, pending human decision — this document is
the evidence for it, not the decision.

## Elapsed time

- Measured: Planning Attempt #11 launched 2026-09-21T09:54:03Z; Project #64
  reached `project:awaiting-acceptance` (all three Stories merged) at
  2026-09-21T16:53:57Z. **Elapsed: ~7 hours.**
- Not measured: total wall-clock time for the full run, including Planning
  Attempts #1–#10 and the preceding Factory capacity-pool/`contract.py`
  fixes (PRs #689, #691, #694, #699, #701, #705). Those occurred earlier in
  the same operating session, but exact start/end timestamps for that
  portion are not available in the evidence this document draws from.

## Model spend

Only costs with a real, provider-reported `exact_cost_usd` value are given.
Nothing here is estimated. Every figure below is reproducible from the raw
`telemetry.jsonl` committed alongside this record under `runs/project64/`
(one subdirectory per run, named identically to the phase it backs) — read
each file's `capacity.route.attempt` record's `usage_receipt.exact_cost_usd`
field directly rather than trusting this table.

| Phase | Attempts | Total cost (USD) | Evidence |
|---|---|---|---|
| Planning, Attempt #11 | 1 | $2.64 | `runs/project64/experimental-career-copilot-64-attempt11/telemetry.jsonl` |
| Planning, Attempts #1–#10 | 10 | not measured (occurred before this evidence window; each ran under the shared $15 Planning cap, but no per-attempt cost figures are available here) | none preserved |
| Delivery, Story #67 | 4 engine invocations, all `capacity.route.final: success` — post-engine failures at different later stages (attempt1: no test command found before the "tests" stage even started; retry: failed the "tests" stage itself; retry2: passed "tests" but failed "acceptance-verification"; retry3: passed everything, PR opened) | $2.67 | `runs/project64/delivery-story-67-attempt1{,-retry,-retry2,-retry3}/telemetry.jsonl` |
| Delivery, Story #68 | 1 | $0.84 | `runs/project64/delivery-story-68-attempt1/telemetry.jsonl` |
| Delivery, Story #69 | 4 engine invocations — the first 3 failed at the capacity layer itself (engine errored, `ambiguous-mutation`/`unknown-failure`); the 4th succeeded at the capacity layer and failed at the Delivery worker's own post-engine "tests" stage instead. The Story was ultimately delivered by hand, not by a 5th paid attempt | $7.49 | `runs/project64/delivery-story-69-attempt1/telemetry.jsonl`, `-attempt1-retry`, `-attempt2`, `-instrumented` |
| Independent Review, PR #70 | 2 (1 initial + 1 re-check after a fix) | $0.40 | `runs/project64/review-pr-70/telemetry.jsonl`, `review-pr-70-recheck/telemetry.jsonl` |
| Independent Review, PR #73 | 1 | $0.17 | `runs/project64/review-pr-73/telemetry.jsonl` |
| Independent Review, PR #75 | 2 completed (real engine cost) + 2 further invocations reported "replay" in this session's own tool output at the time — no capacity-layer call, so no cost | $0.43 | completed: `runs/project64/review-pr-75/telemetry.jsonl`, `review-pr-75-attempt2/telemetry.jsonl`; the two "replay" calls: `review.preparing` fires unconditionally before `review/invoke.py`'s replay check, so the committed `runs/project64/review-pr-75-recheck/process-events.jsonl` and `review-pr-75-fresh/process-events.jsonl` prove only that each execution started, not its outcome — the "replay" result itself is this record's own contemporaneous observation, not independently preserved evidence |
| **Total measured, Attempt #11 onward** | | **≈ $14.64** | sum of the files above |

Not measured: Planning Attempts #1–#10's cost, and the cost of the earlier
Factory-fix PRs (#689/#691/#694/#699/#701/#705) delivered before this
evidence window. Career Copilot Project #64's approved plan carried a $5/60min
spend cap per Story; Story #69 exceeded that cap (~$7.49 against $5) across
its four invocations before being completed by hand instead of a further
paid attempt. **This overrun is not incidental — it is a real, confirmed
Factory control gap, not just a cost fact; see Infrastructure failures
below (ai-software-factory#716).**

## Human interventions / bells

**Formal Factory bells (the two-heading mechanism this repository recognizes):**

1. **Plan approval** — one bell, given in conversation, transcribed and
   posted on Project #64, with the two Issue #37 gaps Planning missed
   (multi-context browser UAT, "New case" clearing chips) corrected before
   posting.
2. **Acceptance** — pending; this document is offered as its evidence, not
   as the decision.

**Ad hoc operator decisions during Delivery** (not a Factory bell each —
ordinary human-in-the-loop authorization during a closely-supervised run,
because Delivery hit real infrastructure failures the standard flow has no
bell for): approximately 15–18 distinct authorizations, including: 3
explicit PR-merge go-aheads (with exact head SHA confirmed each time);
choosing between "quick override" vs. "proper fix" vs. "investigate first"
at each Story #69 failure; authorizing a local Python/browser-tooling
install; authorizing an instrumented diagnostic run; and authorizing the
final hand-delivery of Story #69's fix. None of these carried a `##
Plan approval` / `## Acceptance` heading; none were treated as a substitute
for the two formal bells above.

## Planning attempts

- **Attempts #1–#10**: not detailed here with fresh evidence (occurred
  before this document's evidence window). At a summary level, per the
  session record available: failures spanned a transient network blip, real
  capacity-pool/model-routing defects (stale health records, a permanently
  incompatible model line, an env-var precedence bug), and a sequence of
  real `contract.py`/`prompt.md` alignment gaps (an operating-envelope
  atomicity gap, a verification-action shell-chaining gap, a duplicate
  obligation-assignment gap, and — on Attempt #10 specifically — a
  `_scope_resolves` defect that wrongly rejected a Story's own declared
  creation of a new file in a new subdirectory). Each was fixed via its own
  narrow, hand-authored Story and gated PR before the next attempt.
- **Attempt #11**: the model's raw output was accepted (`capacity.route.attempt`:
  `success`), but the Factory's own deterministic validation recorded
  `capacity.route.final: schema-invalid` / `terminal_outcome:
  validation-failed` — the already-known, human-reviewed OE-RESP-1
  false-positive (backlog ai-software-factory#708: a keyword check misreads
  "no additional network round trip" as a live-provider requirement). This
  was **not** a formally successful Planning attempt in the Factory's own
  telemetry; the plan was activated only by an explicit, narrowly-scoped
  human override waiving that one pre-reviewed finding for this Project's
  plan alone (see Supported overrides below), plus two coverage gaps
  (browser UAT across three answer contexts; "New case" clearing chips)
  corrected by hand in the generated plan before activation — not by
  re-running Planning. Those two gaps existed because Planning was given
  Project #64's own restatement of Issue #37 rather than Issue #37's
  authoritative text; logged as a Factory backlog finding
  (ai-software-factory#710/#712-adjacent territory; specifically the
  requirement-provenance gap, filed separately during this run).

## Delivery attempts by Story

- **Story #67**: 4 engine invocations total (1 original + 3 retries), and
  **every one of the 4 cost real money** ($1.64, $0.33, $0.30, $0.39 —
  $2.67 total; `runs/project64/delivery-story-67-attempt1{,-retry,-retry2,
  -retry3}/telemetry.jsonl`) — the Factory's recovery mechanism restores
  prior work as a fresh starting point, it does not skip paying for the
  agent invocation that inspects and revises it. All 4 succeeded at the
  capacity layer; 3 then failed at a later, post-engine stage (attempt1:
  no test command found; retry: failed the "tests" stage; retry2: passed
  "tests" but failed "acceptance-verification"), and the 4th (retry3)
  passed every stage — tests and all five acceptance verifications — and
  opened the PR. None of the 3 post-engine failures were a real
  implementation defect: this machine had no Python test tooling installed
  at all, and the Delivery worker has no built-in way to detect a Python
  project's test command, so each failed for an environment reason, not a
  code reason, until the environment was fixed.
- **Story #68**: 1 successful attempt, no retries.
- **Story #69**: 4 engine invocations, none reaching a pull request. The
  first 3 failed at the capacity layer itself (`ambiguous-mutation` /
  `unknown-failure` — the engine process errored before finishing), and
  that real failure reason is unrecoverable: the Factory discards the
  diagnostic for this specific outcome (see backlog below). The 4th
  invocation (run with instrumentation added specifically to recover that
  diagnostic) succeeded cleanly at the engine level
  (`capacity.route.final: success`) and reached the Delivery worker's own
  post-engine test-running step instead, which is where the real failure
  was found and fully captured: 176 of 423 tests failing suite-wide. Story
  #69 was ultimately delivered by hand: the AI's own already-correct fix
  (from the 4th invocation) plus two additional fixes found and verified
  by the operator were combined, tested locally and in real CI, and pushed
  as a PR — not delivered by a 5th paid Delivery attempt.

## Infrastructure failures discovered

1. **No Python/pytest test-command support in the Delivery worker.**
   `repository_test_command()` recognizes only `package.json` (npm) or the
   Factory's own internal test script — nothing for Python projects. This
   is the first time the Factory has run Delivery against a non-Node,
   non-Factory repository. Worked around per-run with the existing
   `FACTORY_DELIVERY_TEST_CMD` operator override; not fixed in code.
   Backlogged: ai-software-factory#711.
2. **No Playwright/Chromium available anywhere in the local environment.**
   Worked around by installing `pytest-playwright` and the Chromium binary
   into an isolated virtual environment on the operator's machine, exposed
   to the Delivery worker via a `PATH` override. Not a Factory or product
   code change.
3. **The Factory discards the real diagnostic on an `ambiguous-mutation`
   outcome.** `providers/cli.py` computes a real `diagnostic` string on a
   nonzero exit, but `executor.py`'s `ambiguous-mutation` branch hardcodes
   an empty string instead of using it, and the per-attempt telemetry
   record has no `diagnostic` field at all. This is why Story #69's first
   three failures could not be explained from any log — the information
   never existed after the process exited. Confirmed by reading the code,
   not fixed. Backlogged: ai-software-factory#712.
4. **A hand-created Project can reach Planning but cannot be activated.**
   Project #64 was hand-authored (deliberately, for this real-world
   validation) rather than created through the normal roadmap-commitment
   path, so it lacked the canonical machine-managed sections
   `write_project()` requires. Nothing validates this before Planning
   spends money. Normalized by hand (narrative preserved verbatim,
   canonical sections appended) to unblock this run. Backlogged:
   ai-software-factory#709 (the Factory-side gap) and #710 (the operator
   should have pulled the canonical template rather than inventing a
   structure from memory).
5. **A premature Independent Review verdict is effectively permanent.**
   Triggering review before a required CI check finished produced a
   `findings` verdict (correctly, on the evidence it had) that moved the
   Story back to `story:ready`; re-running review immediately after CI
   finished returned `replay` because no outcome-recording mechanism
   revisits a verdict once made. Recovered by re-claiming the Story and
   pushing a new (empty) commit. Backlogged: ai-software-factory#713
   (corrected once in this same thread after an initial misdiagnosis — see
   that issue's comment history).
6. **A Story's spend cap is not enforced cumulatively across retries.**
   `factory/spec/capacity-pool-integration-plan.md` states the intended
   design explicitly: Story budget applies "across all attempts" and
   "never reset[s]." The actual code does not do this —
   `admission.delivery_request()` re-parses the Story's full declared
   budget fresh on every call, with nothing tracking how much prior
   reservations already consumed. This is a real governance gap, not just
   a cost curiosity: nothing today would have stopped Story #69's 4
   invocations from consuming up to 4x its declared cap even if none of
   them had been an operator-authorized decision. Confirmed against both
   the spec and `admission.py` directly. Backlogged: ai-software-factory#716.

## Product/test implementation failures discovered

Fixed during this run (not backlogged, because they were fixed):

1. **Story #67 — multi-round tool-call turns lost earlier suggestions.**
   `send_message` rebuilt its tool-call list fresh each round and derived
   follow-ups from only the last round, so an earlier round's successful
   call could be silently dropped, or (worse) wrongly re-suggested. Fixed
   by accumulating calls across the whole turn. Regression test confirmed
   to fail against the pre-fix code first.
2. **Story #69 — a documented test exemption was never actually applied.**
   `tests/e2e/test_chat_followups.py` defined `STUBBED_ASSET_HOSTS` to
   exempt chat.html's own Google Fonts request from its "no external
   traffic" check, but the check never referenced that set, so every E2E
   test failed on an expected, already-intercepted request. Fixed.
3. **Story #69 — the test's own network monitoring broke one of its own
   checks.** Playwright's `page.route()` interception, once registered,
   breaks a same-origin `fetch()` issued from `page.evaluate()`, regardless
   of whether the route's predicate matches that URL — confirmed by
   reproducing the identical request succeeding with no route handler
   active. Fixed by using Playwright's separate `page.request` API for
   that one check instead.

Backlogged (real, but explicitly judged non-blocking product-quality edge
cases, not fixed as part of any Story):

- career-intelligence-mcp#71 — a capability that fails mid-turn is not
  recorded as "already tried," so it can be immediately re-suggested.
- career-intelligence-mcp#72 — "build a transition plan" can be offered
  even when zero transition options were actually found.
- career-intelligence-mcp#74 — the session's last allowed turn still offers
  follow-up chips that are guaranteed to fail if clicked.

## Factory fixes made during the run

Two categories, kept strictly separate per standing practice:

**Merged, gated Factory fixes** (before this run's Delivery phase, each its
own hand-authored Story + PR, independent review, tests proven to fail
before the fix and pass after): capacity-pool model registry corrections,
Planning-prompt/`contract.py` atomicity and verification-action alignment
fixes, and the `_scope_resolves` new-subdirectory authorization fix (with
its own three-round review-and-simplify cycle, ending in a deliberately
minimal form per the decision that Planning should not try to predict every
Git/filesystem edge case).

**Not fixed during this run** (explicitly deferred; see Infrastructure
failures above and the Factory backlog list below): the missing Python
test-command support, the discarded `ambiguous-mutation` diagnostic, the
hand-created-Project template gap, and the premature-review-verdict lock.

## Supported overrides / workarounds used

- `FACTORY_DELIVERY_TEST_CMD="pytest -q"` — an existing, built-in operator
  override, used because of ai-software-factory#711.
- `PATH` prepended with an isolated virtual environment containing `pytest`,
  `pytest-asyncio`, `pytest-playwright`, and a downloaded Chromium binary —
  local environment provisioning, not a Factory or product code change.
- A narrow, in-process-only monkeypatch of `contract.validate_output`,
  scoped to swallow exactly one pre-approved, human-waived finding
  (ai-software-factory#708) for Project #64's plan activation alone; never
  written to any file, and contract.py on disk was never modified for this
  purpose.
- A similar in-process-only monkeypatch of `subprocess.run`, used once,
  purely to capture the raw diagnostic text that ai-software-factory#712
  otherwise discards; not persisted.
- Story #69's final delivery was pushed by the operator directly (a real
  branch, a real PR, real CI, real Independent Review) rather than through
  a 5th paid Delivery attempt, after 3 attempts failed at the capacity
  layer and a 4th failed at the Delivery worker's own post-engine test
  stage, with the underlying cause fully root-caused and fixed by hand.

## Independent-review findings

**Blocking, fixed before merge:**
- PR #70 (Story #67): the multi-round accumulation bug (above). Fixed,
  re-reviewed, re-approved on the fixed commit before merge.
- PR #75 (Story #69): a premature `findings` verdict citing missing CI
  evidence (a timing issue, not a code defect — see Infrastructure failures
  above). Resolved by re-running review once CI genuinely completed.

**Non-blocking, correctly deferred to backlog** (both Factory's own
Independent Review and the product repository's own separate Codex review
were used; findings from either are listed above under their matching
category, not duplicated here).

**Not real, correctly dismissed:** one finding on PR #70 ("browser users
can never see these chips") reflected a misunderstanding of Story #67's
deliberately narrow scope (chip UI wiring is Story #68's job) rather than a
real defect; verified against the approved multi-Story plan and dismissed
with reasoning, not silently ignored.

## E2E / browser evidence

Real, not simulated: `tests/e2e/test_chat_followups.py` runs headless
Chromium via `pytest-playwright` against the actual FastAPI webapp (stubbed
chat backend, no `OPENROUTER_API_KEY`), and passed in real GitHub Actions CI
on the final merged commit — confirmed directly against the GitHub check-run
API, not inferred. It proves: chips render and a click round-trips as a new
user message; three distinct answer contexts render three distinct,
allowlist-valid chip sets; "New case" clears chips; and the page produces
zero console errors, zero failed/non-2xx requests, and no non-localhost
network traffic (including an explicit favicon check), all with no
live-provider credential present.

## Final CI / full-suite evidence

`career-intelligence-mcp`'s `master` branch, commit
`8920418df7c518c3b3745ce8bfda479cf01eb97a`: the repository's own required
"test" check (`pytest -q`, the same single command used throughout, per
`.github/workflows/tests.yml`) — completed, success. Checked directly
against the commit's check-runs, not against any PR's self-reported status.

## What worked well

- Running two independent reviewers (the Factory's own Independent Review
  and the product repository's own separate Codex integration) caught real
  defects the other missed, in both directions, across every PR this run.
  Neither was treated as sufficient alone.
- The Delivery worker's recovery-checkpoint mechanism correctly preserved
  real, valid work across repeated capacity-layer failures on Story #69,
  so no AI spend was wasted re-deriving code that was already right.
- The standing discipline of stopping to verify a claim against actual
  execution evidence — rather than accepting a summary or a generic error
  label — is what turned an unexplainable "unknown failure" into a fully
  understood, fixed, and verified root cause for Story #69.
- The bounded-correction discipline (never silently widening scope; always
  stopping to report and let the human decide) correctly prevented at least
  one wrong "fix" (an incorrect `contract.py` change earlier in the session,
  caught before merge) and kept every subsequent correction narrow and
  verified.

## What caused the most operational friction

- **Story #69's cost and time were dominated by environment gaps, not code
  quality.** Of the ~$7.49 spent across 4 Delivery invocations (3 capacity
  failures, 1 that reached and failed a post-engine test stage), the
  actual defects found (two small test bugs) would ordinarily cost a
  fraction of that to fix — the expense came from this being the Factory's
  first-ever real Delivery run needing Python test tooling and a browser at
  all, with no built-in support for either.
- **A discarded diagnostic turned a five-minute problem into a two-hour
  investigation.** Had `ambiguous-mutation`'s real error text been
  preserved (ai-software-factory#712), the pytest-playwright /
  pytest-asyncio conflict driving Story #69's repeated failures would have
  been visible on the very first attempt.
- **A hand-created Project's structural gap was invisible until the most
  expensive possible moment** — after Planning had already run and spent
  real budget, rather than at Project creation.

## Measurement integrity

`CLEAR — implementation can proceed`

Checked before writing the recommendations below, per the retrospective
skill's mandatory Measurement Integrity Check: searched open Factory issues
for an active official qualification/benchmark/Rung run. None is in
progress — the only Rung-related open items are Story #644 (independent
root-cause review of five *past, already-failed* Rung 2 runs — retrospective
analysis, not a live measurement) and #541/#451 (Rung 2 retrospective and
starting-portfolio planning, both dormant). No evidence of a currently
active controlled measurement that today's Delivery work on Project #64 or
the recommendations below could contaminate.

This does not by itself authorize implementing anything: per Mahesh's
explicit instruction, no backlog remediation starts as part of this record
or Project #64's acceptance. The recommendations below are queued findings,
not authorized work.

## Next improvement

**Primary: fix ai-software-factory#712 (preserve the real diagnostic on an
`ambiguous-mutation` outcome).** Earliest preventable root cause: this one
gap is why Story #69's first three failures (of its real $7.49 spend, by
far the larger waste this run) could not be diagnosed from any log at all,
turning what should have been a five-minute read into a multi-hour
instrumented investigation. Fixing it does not just help the Python
test-command case below — it makes *every* future `ambiguous-mutation`
failure, for any reason, diagnosable on the first occurrence. It is a
small, low-risk, already-precedented change (an equivalent diagnostic-
surfacing fix was made earlier this same session for a different capacity
outcome), with no dependency on the other findings below.

**Do not change yet:** the five secondary items below, including
ai-software-factory#711 and #716. Fixing #711 is real and worth doing, but
it is narrower: it would have prevented only the genuinely wasted retry
invocation(s) on Story #67 (the one that failed the "tests" stage outright
— on the order of $0.33, not the invocation's full $2.67, since the first
invocation's $1.64 was necessary productive work regardless, and the final
successful invocation's cost was also necessary, not waste a detector
would remove). #716 (spend cap not enforced cumulatively) is genuinely
more severe in kind — it is an authorization-boundary gap, not just a
cost/diagnosability one — but is architecturally larger (it needs
cumulative-spend tracking across reservations, not a single small code
change), so it is queued rather than primary; it should not wait
indefinitely given its severity. Do not generalize any of these fixes
beyond its demonstrated case (Python for #711; the one `ambiguous-mutation`
branch for #712) — solve the demonstrated problem first, per the
retrospective skill's preference against premature generalization.

**Secondary, queued, not prioritized further than their order below except
where noted:**

1. Fix ai-software-factory#716 (Story spend cap not enforced cumulatively
   across Delivery retries) — a real authorization-boundary gap, not just
   a cost curiosity; queued ahead of the items below despite the larger
   implementation effort, given its severity class.
2. Fix ai-software-factory#711 (Python/pytest test-command detection) —
   prevents recurrence for the next Python (or other non-Node, non-Factory)
   Delivery run specifically; real, but smaller demonstrated cost impact
   than #712 above.
3. Validate a Project's canonical section structure before Planning runs
   against it (ai-software-factory#709/#710), so a hand-created or
   otherwise malformed Project fails cheaply at onboarding rather than
   after a paid Planning attempt.
4. Decide and document the intended relationship between Independent
   Review timing and required-check completion (ai-software-factory#713):
   either the operator convention should be "never trigger review before
   required checks complete," or a stale `findings` verdict should be
   revisable once its stated missing evidence later appears.
5. Revisit whether Planning's `_scope_resolves` (and similar validators)
   should keep trying to predict Git/filesystem/parser edge cases at all
   (ai-software-factory#706/#707) — three consecutive review rounds each
   found a new one this session, which is itself evidence the approach does
   not converge, matching the decision already reached mid-run to keep that
   validator deliberately minimal.

## Validation

The next real Delivery attempt that fails with an `ambiguous-mutation`
outcome, for any reason, will demonstrate whether the primary fix worked:
the resulting `DeliveryError`/log should carry the real diagnostic text
from the underlying CLI failure, not an empty string. A recurrence of
today's exact symptom — a failure with no diagnostic text recoverable from
any log — would falsify the fix.

Separately, and only once ai-software-factory#711 is picked up: the next
real Factory Delivery run against a **Python** repository specifically
(not Ruby, Go, or another language the fix is not meant to cover) will
demonstrate whether that fix worked: Delivery should reach the worker's
own "tests" stage on its first engine invocation, with no
`FACTORY_DELIVERY_TEST_CMD` operator override needed
and no capacity-layer retry caused by a missing test command. A recurrence
of today's exact failure ("repository declares no supported test command")
on that next run would falsify the fix.
