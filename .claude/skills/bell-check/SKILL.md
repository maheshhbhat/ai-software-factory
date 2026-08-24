---
name: bell-check
description: Review something that needs a human decision — a project's criteria, an acceptance claim, a story, a PR, or a script an agent asked you to run — and report in plain language what is broken or unproven. Verifies claims against the repository rather than trusting the summary. Use before ringing any bell, or whenever an agent says something passed.
---

# Bell check

Someone is about to make a decision. Your job is to find what is wrong with the
thing they are deciding about, verify it against the repository, and say it in
words they can act on.

Two failure modes to avoid, both observed here:

* **Trusting the summary.** An agent produced a script that printed `pass` next
  to fourteen criteria without running a single check, and asked for approval.
  The claim and the evidence were unrelated. Read the mechanism, not the report.
* **Burying the answer.** A correct review nobody can read is a review that did
  not happen. Two things are wrong out of sixteen — say which two, first.

## What to check

Run these three questions against whatever you were given. They are ordered:
question 1 usually settles it.

**1. Show me where.** For every claim, find the artifact — a file path, a PR
number, a label timestamp, a test name. A claim whose evidence is another claim
is unverified. Read the actual file; do not accept a description of it.

**2. What would have failed?** If no input could make this check report a
failure, it is not a check. Look for the branch that produces the negative
result. If there isn't one, say so.

**3. Is the claim narrower than it sounds?** Big statements usually have small
print. "Zero relay" meant six minutes of one story, not the phase. Find the
scope the claim actually covers and name the gap.

## Verify against the repository

Never repeat an agent's claim without checking it. Concretely:

```bash
# what a label history actually did — not what a summary says it did
gh api repos/OWNER/REPO/issues/N/timeline --paginate \
  --jq '.[] | select(.event=="labeled") | "\(.created_at)  \(.label.name)"'

# did the checks really pass
gh pr view N --repo OWNER/REPO --json state,mergedAt,statusCheckRollup

# does the approved checklist still match the live body
gh issue view N --repo OWNER/REPO --json body,comments
```

Read scripts before running them. Check that a file an agent claims to have
changed actually contains the change. Compare timestamps: if a decision landed
under a minute after the thing it approved, nobody read it.

## Bells this repository has

`plan-approval` · `acceptance` · `hazard-ack` · `poison-rescue` ·
`scope-decision` · `sampling` · `cutover-approval`

Pull requests need no human approval — the merge gate decides. If someone asks
for a PR to be approved by hand, the question is why the gate did not.

For an **acceptance** bell, add one check: **is the evidence posted on the
issue?** Evidence generated into `runs/` and left there has twice reached a bell
with nothing for the decider to read.

## How to report

Plain language. Short sentences. The reader is a delivery manager, not the
author of the code.

* **Lead with the verdict.** One line: is this signable or not, and why not.
* **Name what is broken. Skip what is fine.** Two bad criteria out of sixteen
  means two paragraphs, not sixteen.
* **No section references unless asked.** Write "the rules say a project can't
  be accepted until every story is done", not "§4.1 line 231".
* **Quote the evidence inline** — the timestamp, the file line, the exact string.
* **End with the decision they face**, stated as a choice, not a recommendation
  dressed as a fact.

Say what the thing *is* before saying what is wrong with it. A reader who does
not know what they are looking at cannot judge a finding about it.

## Rules

**Never ring the bell.** Do not post a comment beginning `## Plan approval` or
`## Acceptance`, and never write a `decision:` or `result:` field, unless the
owner has stated the verdict and asked for it to be posted. Those two headings
are how the runtime recognises a human decision. Post review input under any
other heading.

**Never say something passed that you did not check.** "Not verified" is a
useful answer. A confident wrong answer is worse than an admitted gap.

**Report the gap you found in your own work too.** If you wrote the thing under
review, apply the same three questions to it and say what they surface.
