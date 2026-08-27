# Project #67 / Story #70 review retrospective

## Verdict

Story #70 was poisoned by a mixed failure. PR #73 had two genuine current-Story test gaps, but independent Review also charged Story #70 for real-Chrome work that the approved plan assigns to dependent Story #71. The factory supplied the reviewer the whole Project checklist without supplying the sibling-Story topology that explains when each item becomes due.

This is a factory control failure, not evidence that the Project should be cancelled. Rescue is appropriate only after the reviewer receives the missing plan context.

## Durable evidence

- Product Project #67 declares Stories #69, #70, and #71 in that dependency order.
- Story #70 owns browser rendering and stale-output clearing. Its attempt counter reached `3` and its durable state became `story:blocked:poison`.
- Story #71 depends on #70 and owns browser-level representative timing plus the owner-executable Chrome procedure after all delivery PRs merge.
- Product PR #73 reached head `45f6919282a49d26528c8d0983badfc1f5df7af2`. Its trusted `tests`, `merge-gate`, and `merge-gate-surface` checks passed.
- Story #70's three `## Review findings` comments demanded owner Chrome evidence on every head. The final review additionally identified two current-Story gaps: the test used a hand-written mock DOM instead of the promised browser flow, and it counted three rendered results without asserting the exact 2%, 3%, and 4% headings.
- `factory/agents/review/invoke.py` supplies `story_spec`, the complete `project_criteria`, operating-envelope obligations, ADRs, and the PR diff. It does not supply sibling Stories, their dependencies, their lifecycle states, or requirement ownership.
- `factory/agents/review/prompt.md` tells Review to evaluate the exact head against Project criteria, but does not distinguish current obligations from planned future obligations.

## What would have failed

The existing reviewer-input tests would not fail if sibling Stories disappeared, if a future Story owned a Project criterion, or if Review demanded that future work from the current Story. No test represented the #70/#71 dependency boundary.

PR #73's own tests would fail if stale comparison output remained after invalid input, but they would not fail if the three headings were incorrectly labeled. They also execute a registered callback against a hand-written mock DOM, so they do not prove real page behavior.

## Five whys

1. Story #70 failed review because Review found the exact-label and real-browser evidence insufficient and also required owner Chrome evidence.
2. The worker did not supply owner Chrome evidence because Story #71 owns that work and cannot start until Story #70 completes.
3. Review demanded it because Story #70's wording says `browser-flow`, the Project checklist contains browser and Chrome requirements, and Review could not see the sibling Story that owns the later evidence.
4. Review could not see that boundary because its serialized input contains the current Story and whole-Project criteria, but not the approved Story topology or requirement ownership.
5. The factory allowed the mismatch to consume all attempts because reviewer findings are free-form natural language; no deterministic boundary validates a finding before returning the Story to delivery.

## Classification

### Genuine Story #70 defects

- Assert the rendered summary and projection headings are exactly 2%, 3%, and 4%, with no additional scenario.
- Satisfy the Story's promised browser-flow stale-output observation or obtain an explicit human scope correction if the zero-dependency product cannot provide that observation inside the approved scope.

### Future Story #71 work incorrectly charged to Story #70

- Representative real-browser click-and-render timing.
- Page-console assurance in the completed browser flow.
- The owner-executable Chrome procedure and the owner's post-merge evidence record.

## Lesson

More prompt text alone is useful but insufficient. Short-term recovery needs the complete approved Project plan in Review's input. Durable prevention needs structured requirement ownership and a deterministic boundary around the AI finding before a finding can spend another delivery attempt.
