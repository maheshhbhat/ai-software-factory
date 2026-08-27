# Project 67 Story 71 retry retrospective

## Verdict

The qualifying Rung 2 repeat failed with findings. Story 71 did not prove its
required real-Chrome assurance, and its final delivery retry did not receive
the human correction context that was written specifically for that retry.
Project 67 remains incomplete. This retrospective is not an acceptance record
and does not rescue, cancel, merge, or otherwise dispose of product Story 71.

## What the product check exposed

Product PR 74, at head
`9ef7b45c2f7f83ce6e7b0de2be4638229babf132`, contains a Chrome test that reads
JSON from a hidden result element. On the exercised machine the element was
empty. `JSON.parse` raised `Unexpected end of JSON input`.

The exact-head test converted that error into a skip, so it did not prove the
required browser-level click-to-render check. A local diagnostic reproduced an
exact empty string even after the Chrome virtual-time budget was increased from
one second to ten seconds. This is a genuine product harness defect. It is not
evidence that the comparison feature itself is slow or incorrect.

Attempts 2 and 3 made the Chrome failure enforceable, then failed the product
suite at `test/comparisonDisplay.test.js:255`. The wrapper correctly refused to
commit or push failing work. PR 74 therefore remained on its original head.

## What the factory omitted

Before Attempt 3, durable correction records existed in two places:

- Story 71 had the original `## Review findings` comment.
- Story 71 later had `## Human review input` with the reproduced empty-result
  evidence and the explicit completion-signal recommendation.
- PR 74 had Mahesh's transcribed `## Request changes` decision.
- Story 71 had `## Final bounded correction authorized`, preserving Attempt 2
  so the next dispatch would be the third and final attempt.

The current delivery input does not assemble that correction history.
`factory/agents/worker/invoke.py::build_input` fetches Story comments, then keeps
only comments whose body contains the literal heading `## Review findings`.
It does not read PR comments for this purpose. Attempt 3 therefore received the
original AI finding but not the later human diagnosis, request-changes decision,
or final-attempt correction target.

The delivery prompt already says a retry must address attached findings. The
missing behavior is upstream: the relevant records were not attached.

## Why the distinction matters

Two failures occurred and neither should hide the other:

1. The product Chrome harness did not produce browser evidence.
2. The factory discarded relevant retry guidance before invoking the worker.

Fixing only the product would leave the factory unable to learn from a human's
more precise diagnosis. Fixing only the routing would not make PR 74 pass. The
factory correction must be verified independently, then a new comparable Rung 2
Project must test whether the corrected factory can carry review feedback into
a later attempt.

## Controls that worked

- The reviewer rejected a skippable browser check.
- The product wrapper ran the real repository suite without engine credentials.
- Failing tests prevented a commit and push.
- Attempt 3 was the final dispatch; no automatic reset or fourth attempt
  occurred.
- The poller was stopped after the bounded failure.

These controls stay. The correction adds context; it does not weaken any gate
or enlarge the retry budget.
