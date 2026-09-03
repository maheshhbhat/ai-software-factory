# Phase 5 Rung 2 — independent root-cause review (2026-09-03)

Reviewed from repository evidence only: the five frozen closeout bundles under
`runs/rung2/`, `factory/spec/phase5-issue-log.md` (P5-001 to P5-053), the three
retrospectives in `factory/learning/`, the touch log, and the git history of
`origin/main`. This review does not assume the factory's own classifications
are correct; each incident below was classified independently by the
component that owned the defect.

## Verdict

Rung 2 has not failed five times for five different reasons. It has failed for
two, and neither has been fixed.

First: Planning keeps writing acceptance criteria that the factory's own
delivery pipeline cannot verify — "real Google Chrome", "no console errors",
"responsive under one second." Second: the delivery engines running on the
operator's Mac fail to start, or fail partway through, often enough that the
retry budget is spent on infrastructure, not on code. Every rescue those two
causes force is counted against autonomy. The 53 logged fixes in the issue log
were aimed almost entirely at the readiness doctor, the harness, and the
adapters — the places the symptoms surfaced — and the two owners above were
never changed. Another qualification run on this configuration would likely
produce the same verdict.

## What was actually run

Five projects reached a frozen KPI bundle. Three more were started and
abandoned before a bundle closed (#30 live-data defect, #47 performance
freeze, #67 review-boundary failure). All eight targeted the same product
repository, `income-portfolio-analyzer`, with a small retirement-withdrawal
feature each time.

| Project | Dates | Stories | Attempts | Autonomous | Poisoned | Human recovery | Product | Integrity |
|---|---|---:|---:|---:|---:|---:|---|---|
| #18 | 24-25 Aug | 4 | 11 | 2 (50%) | 2 | 2 | accepted | pass |
| #60 | 26-27 Aug | 3 | 8 | 1 (33%) | 1 | 2 | accepted | pass |
| #76 | 27-28 Aug | 3 | 8 | 2 (67%) | 1 | 1 | accepted | pass |
| #89 | 30 Aug-1 Sep | 3 | 16 | 1 (33%) | 2 | 2 | failed with findings | fail |
| #100 | 1-3 Sep | 3 | 11 | 0 (0%) | 1 | 3 | failed with findings | fail |

Across the five runs: 16 stories, 54 worker attempts, 14 merged (all worker-
authored), only 6 delivered without a human touching them (37.5%, against a
75% bar). Relay stayed at zero in every run — the state machine and human-
decision plumbing held. The gap between "merged" and "merged autonomously" is
almost entirely made of infrastructure recoveries and criteria the factory
could not prove on its own.

## Incident classification

Each story that needed a human is classified by the component that owned the
defect, not the component where it was noticed and not the factory's own
label.

| Story | What happened (evidence) | Owner | Defect or environment |
|---|---|---|---|
| #18 S20 | Attempt 1: worktree creation refused under `.git/worktrees` before Codex started (P5-023). Attempt 2: nested Codex could not initialise its app-server client (P5-025). Attempt 3: Claude finished the code, then the wrapper ran the factory's Python test script inside the JavaScript product checkout and discarded a valid change (P5-027). Poisoned, rescued, merged on attempt 4. | Runtime / environment | Zero engineering failures; three infrastructure failures consumed the whole budget. |
| #18 S21 | Two attempts hit the $5 cap without producing a PR ($5.37, $5.24 — P5-035/036: headless mode could not run shell, effort unset). Third: HTTP 429 "session limit" at 435s (P5-037). Poisoned, rescued, merged after $4.52. | Runtime / environment | $13.58 spent on attempts that could not have succeeded as configured. |
| #60 S62 | Four claims, three engine completions, one rescue. | Insufficient evidence | The frozen bundle holds only `story.claimed` events; the cause is not in the repository. |
| #60 S64 | Merged only after "exact-head lifecycle repair," "owner Chrome verification," and an evidence-transcription correction (operator actions). | Planning | Criteria required a human in Chrome — a criteria-design defect, not code. |
| #76 S80 | Poisoned, then "scope correction adding app.js," then rescue, then merged. Story scope had omitted the file the feature needed to change. | Planning | Scope declared incorrectly at planning time; three attempts spent discovering it. |
| #76 project | Acceptance needed "owner Chrome click-and-render verification" as an operator action. | Planning | Same criteria pattern as #60. |
| #89 S91 to S95 | S91 poisoned three times and was cancelled; Planning had no route to replace one story (P5-047), then the doctor refused the replacement twice (P5-048, P5-049). S95 merged after another rescue. | Governance (recovery route); S91 cause insufficient | The lifecycle could stop unsafe retries but not recover cleanly; two doctor defects blocked the recovery route it had just been given. |
| #89 S93 | Planning required "real Chrome responsiveness and console-error assurance" while forbidding a browser-test dependency and limiting the story to one test file. The worker built a custom dump-DOM harness that never produced trustworthy evidence. 16 attempts, two rescues, cancelled. | Planning | Criteria were unsatisfiable inside the declared constraints — the factory's own note calls this "factory-systemic." |
| #100 S102, S103 | Both merged in one attempt each, then Planning changed an already-merged label during a re-plan and an operator had to restore lifecycle state on both. | Planning | The planner wrote to stories it did not own; two clean deliveries were scored as needing recovery. |
| #100 S104 to S107 | S104 required adding `@playwright/test`, which an existing product test forbids; three attempts hit the same wall (P5-052). Cancelled and replaced by S107, which needed nine launches (below). Not merged. | Planning, then environment | Planning did not read the repository's existing constraints; then the environment could not run branded Chrome. |

### Story #107 — the last run, launch by launch (2-3 Sep, UTC)

This is the most complete record in the repository and the clearest picture
of what the retry budget actually buys.

| Time | Engine | Result |
|---|---|---|
| 22:23 | claude-fable-5 | failed after 120s, "schema-invalid," no usage reported |
| 22:30 | claude-fable-5 | failed after 210s, "started-mid-work-failed," no usage reported |
| 23:58 | claude-fable-5 | failed after 80s, "started-mid-work-failed," no usage reported — poisoned |
| 00:40 | rescue 1 | after factory PR #638 merged mid-run |
| 00:53 | claude-opus-4-8 | failed after 349s, "started-mid-work-failed," no usage reported |
| 00:57 | claude-opus-5 | failed after 219s, files already edited ("post-mutation"), no usage reported |
| 01:04 | gpt-5.5 | engine completed (1.68M input tokens); product tests failed in 4.5s — poisoned |
| 02:35 | rescue 2 | after factory PR #640 merged mid-run; story clarified to "headless branded Chrome" |
| 02:45 | gpt-5.6-sol | engine completed; PR #108 opened; independent review returned findings at 02:48 |
| 02:49 | claude-fable-5 | failed, post-mutation; operator stopped the run after repeated macOS Chrome crash pop-ups |
| 02:55 | claude-fable-5 | failed after 59s, post-mutation, no usage — budget exhausted; project marked failed |

Seven of nine launches never returned a usable engine result. All seven were
routed to Anthropic models; both launches routed to OpenAI models completed.
In three of the seven the worker had already edited files when it died — yet
the poller's durable record for every one reads "a definite failure proves
the worker did not start, so trying another is safe," while the telemetry for
the same launch records `terminal_outcome: started-mid-work-failed`. The
runtime is contradicting itself about the one fact the retry policy depends
on.

Source: `runs/rung2/project100-final/telemetry.jsonl` (`capacity.route.final`,
`engine.usage`) and `process-events.jsonl` (`worker.failover`,
`worker.launch.end`) on branch `codex/project100-phase5-closeout`.

## Root causes

Ordered by how many of the 16 stories each one touched.

### RC-1 — Planning: acceptance criteria demand evidence the factory cannot produce

Owner: Planning. Touched at least six stories (S64, S70, S71, S93, S104, S107)
across 5 of 8 projects.

"Real Google Chrome," "no page-generated console errors," "renders in under
one second," "owner click-and-render verification" appear in the criteria of
#60, #67, #76, #89, and #100. None of the factory's runners — worker wrapper,
independent reviewer, merge gate — can execute a branded Chrome session on the
operator's Mac from inside a headless agent; Project #100 proved that branded
Chrome crashes during macOS application registration when launched under the
Codex app, and that the same headless channel works in GitHub Actions. So
every project ends the same way: either a human opens Chrome (#60, #76 —
scored as recovery) or the story burns its attempts trying to fake the proof
(#93: sixteen attempts on a hand-built DOM dumper; #107: nine launches).

This is a defect in the criteria contract, not in delivery. A criterion the
factory cannot check is a bell in disguise. The fixes that followed —
reviewer context (#67), envelope obligations, replacement routes, Playwright
in the product — were all placed downstream of the planner. The planner's
prompt and contract were never given the rule "every criterion must name the
runner that will check it, and that runner must exist."

### RC-2 — Runtime and execution environment: the Mac is not a stable delivery substrate

Owner: Runtime / adapters plus the execution environment. Touched at least
four stories and at least 15 of 54 attempts.

Counted from the frozen bundles and the issue log: worktree permission error
(P5-001, 002, 023), nested Codex unable to initialise (P5-025), Claude
headless mode unable to run shell (P5-035), $5 cap reached before a PR twice
(P5-036), HTTP 429 session limit (P5-037), reviewer killed at exactly 60s
twice (P5-030), reviewer token leaking into delivery (P5-028), factory test
wrapper run against a JavaScript repo (P5-027), five consecutive
Anthropic-routed launches on Story #107 dying without reporting usage, branded
Chrome crashing under the Codex app (#107). In Project #18, $17.88 of the
$31.52 known spend — 57% — went to attempts that could not have succeeded as
configured.

Some of this is environment (sandbox permissions, session limits, Chrome
under a GUI agent). Some is adapter defect (wrong test command, token
leakage, effort unset, timeout equal to the inner timeout). The factory's own
classification tends to record these as "fixed locally" one at a time; what
it has not recorded is the aggregate: the delivery path has never had a
measured launch-success rate, and every Rung 2 run has been the first live
test of whatever adapter change was merged the day before.

### RC-3 — Governance: attempts are spent on non-attempts, then scored as autonomy failures

The state schema says infrastructure failures do not increment the attempt
counter. The record shows they do, repeatedly: worktree creation failures,
engine initialisation failures, no-capacity launches (Story #58, retrospective
finding F5), wrapper misconfiguration, and launches the poller itself
mislabels as "did not start." Each such attempt walks a story toward poison;
each poison forces a rescue; each rescue is a human touch; each touch removes
the story from the autonomy numerator. The KPI is therefore mostly measuring
infrastructure flakiness plus criteria the factory cannot prove, and only
incidentally measuring whether agents can deliver software. The capacity-pool
reservation added between runs was meant to fix this and did not: #107 still
consumed nine attempts, five of them on engines that produced nothing.

### RC-4 — Governance: the system under test changed continuously during qualification

Between the first Rung 2 run (24 Aug) and the last (2 Sep), `origin/main` took
146 commits across 48 merged PRs: 11,594 lines added under `factory/`,
including an entirely new subsystem (`factory/capacity_pool/`, 11 files),
2,065 lines in the runtime, 3,798 in the acceptance harness. Four factory PRs
(#634, #636, #638, #640) merged during Project #100's lifetime, three of them
between Story #107's launches. No two runs tested the same factory, so the
five verdicts are not five samples of one system; they are one sample each of
five systems. That is why the autonomy series (50%, 33%, 67%, 33%, 0%) does
not trend.

The readiness doctor is the sharpest example. Eleven of the 53 logged findings
are defects in the doctor itself (P5-001, 007, 011, 012, 025, 038, 044, 045,
046, 048, 049), including five in a row (P5-044 to P5-049) where each fix to
the doctor was blocked by the next doctor defect. A safety check that grows
modes and fails more often than the thing it guards has become a source of
downtime rather than a control.

### RC-5 — Planning: does not validate against the repository it plans against

Owner: Planning. Touched at least four stories/projects. #76 Story 80's scope
omitted `app.js`. #100 Story 104 required a dev dependency that an existing
product test explicitly forbids — three attempts hit the same assertion. #47
specified cent-level search with no operating envelope, freezing the browser
at a realistic portfolio size. #30 accepted fixture evidence for a
live-provider claim. #100's re-plan rewrote labels on two already-merged
stories. These are five instances of one defect: the planner emits scope and
criteria without validating them against the repository's existing files,
tests, and lifecycle state. The responses were an owner-cancelled-replacement
route, an envelope artifact, and a doctor mode — all mechanisms for
recovering from bad plans, none for producing good ones.

### RC-6 — Runtime: the frozen bundles do not preserve failure causes

Three of five bundles (#18, #60, #76) contain only `story.claimed` events — no
worker outcomes, no review verdicts. In the two that do carry outcomes, the
failure detail is truncated to roughly 200 characters and always reads
"activity started," so the actual error is gone. Cost is a lower bound or
unavailable in all five runs (subscription-backed engines report no price;
Anthropic failures report no usage at all). Cycle time is "unavailable" in
two. Measurement integrity failed in the last two runs. The repository cannot
currently answer "why did Story 62 poison?" — and a qualification programme
that cannot answer that question for its own runs is not measuring; it is
narrating.

## Were the fixes aimed at the owner?

Mostly not. The pattern is consistent: a story fails, the failure surfaces in
the doctor, harness, or adapter, and that component gets patched. The
component that caused the failure is largely untouched.

| Failure pattern | Owner | Where fixes landed | Hit the owner? |
|---|---|---|---|
| Unverifiable Chrome/console/timing criteria (#60, #67, #76, #89, #100) | Planning contract | Reviewer context, envelope obligations, Playwright story in product, replacement routes, doctor modes | No — planner prompt and output contract unchanged on this point |
| Engine launch failures on macOS (#18, #89, #100) | Environment + adapters | Doctor probes, capacity pool, failover messages, effort default, timeouts | Partly — adapters patched; environment never changed |
| Infra failures consuming attempts (#18, #58, #100) | Attempt accounting | Capacity reservation before claim; repair-claim command | Partly — #107 still spent 7 of 9 attempts on engines that produced nothing |
| Scope/dependency conflicts (#76, #100, #47, #30) | Planning validation | Owner-cancelled replacement route, envelope artifact | No — recovery routes added; no pre-approval validation against the repo |
| Doctor refusing valid runs (five findings in a row) | Doctor design | More doctor modes and exclusions | No — complexity added to the failing component |
| Lost failure evidence (#18, #60, #76, #107) | Runtime logging | PR #640, "preserve both ends of failure diagnostics" — merged mid-run, after evidence was already lost | Late |

## Where the evidence is insufficient

The cause of Story #62's poison (#60) and Story #91's three poisons (#89) is
not in the repository; the bundles hold only claim events and the issue log
does not mention them. The full engine error for any of Story #107's seven
failed Anthropic launches is not recoverable — the durable detail is
truncated to the first log line. Whether the Anthropic-route failures were
quota, auth, sandbox, or adapter cannot be determined from the repo; the
pattern (0 of 7 vs. 2 of 2 for OpenAI) is strong but the mechanism is
unproven. The independent reviewer's approval of PR #98 at 00:36 on 1 Sep sits
after a `review_link.declined` record at 00:34 saying the same PR was already
closed — an ordering anomaly this review cannot explain. Real dollar cost is
unknown for every run except as a lower bound. None of this changes the root
causes above; it does limit how precisely RC-2 can be attributed.

## What is not broken

The state machine, the merge gate, the human-decision plumbing, and the touch
log. Relay was zero in all five runs. Every merged PR carried exact-head
review, green checks, and the required gate. Every human decision has a
comment, a timestamp, and a receipt. The independent reviewer caught the
missing-resource 404 on PR #108 before merge — an acceptance catch, working
as designed. The part of the factory that was hardest to build is the part
that works.

## Recommendation

Do not run another Rung 2 on this configuration. The next run would sample a
sixth different factory, on the same unstable substrate, against the same
unverifiable criteria, and would likely fail for the same two reasons. Do the
following instead, in order, treating each as a gate:

1. **Freeze the factory.** Tag `origin/main`. No factory PRs merge between the
   start and end of a qualification run. A run during which the factory
   changes is discarded, not rescued.
2. **Qualify the substrate before qualifying the factory.** Push twenty
   trivial, identical stories (a one-line change with a one-line test)
   through `poll.sh` on the intended host. Measure launch success per route,
   engine completion rate, and attempts per story. Set the bar at 95% or
   higher first-attempt completion. Until this number exists, no Rung 2
   result is interpretable. If the Anthropic route stays near 0 of 7, remove
   it from the pool or move delivery off the Mac — #100 already showed the
   Linux runner in GitHub Actions can run the Chrome channel the Mac cannot.
3. **Change the criteria contract at the planner.** Every acceptance
   criterion must name the runner that checks it (product test suite,
   Playwright in CI, merge gate, or "human bell — counted"). A criterion with
   no runner is rejected at plan approval. Drop "real Google Chrome" from the
   vocabulary unless the CI runner executes it.
4. **Make the planner read the repository.** Before a plan reaches approval,
   validate declared scope against the files the change must touch and
   declared dependencies against existing tests and manifests. Two of five
   runs died on exactly this.
5. **Fix the attempt ledger and define "autonomy."** An attempt is counted
   only when an engine returns a result the wrapper can test. Launch
   failures, capacity failures, and mislabelled "did not start" outcomes
   reset rather than increment. Report autonomy twice: engineering autonomy
   (stories merged without a human touching code or criteria) and
   operational autonomy (no human touching anything). The current single
   number conflates them and hides which one is failing.
6. **Fix evidence before the next run, not during it.** A bundle that lacks
   worker outcomes, review verdicts, and the full terminal error for every
   failed launch does not close; the run is INCONCLUSIVE.
7. **Then** run one more Rung 2 — a fresh feature of comparable difficulty, on
   the frozen tag. If it fails, the failure will finally be about the
   factory.

## The decision

Either (a) spend the next block on steps 1-6 with no product run and no
factory feature work, or (b) accept Rung 2 as a documented failure, record
the two root causes as the finding, and re-scope the ladder around a Linux
delivery host and machine-checkable criteria before Rung 3 is even planned.
Both are defensible. Running a sixth attempt as things stand is not.
