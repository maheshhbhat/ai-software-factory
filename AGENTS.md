# Agent instructions

Read this before working in this repository. It applies to **every** engine —
Claude, Codex, or whatever `FACTORY_WORKER_ORDER` names next.

Some of this repository's working rules live in `.claude/skills/`, which only
Claude Code discovers automatically. A rule that binds one engine and not
another is not a rule, it is a coincidence of which worker got dispatched. So
the rules that must hold regardless of engine are restated here.

## Monitoring a factory run

When asked to monitor a black-box UAT or factory delivery, read
`.claude/skills/factory-monitor/SKILL.md` and use its bundled script. This is a
plain repository skill shared by every engine; do not replace it with ad-hoc
`tail`, JSON parsing, or GitHub polling commands.

## Reviewing anything that needs a human decision

**Do this automatically, without being asked**, whenever: a project sits at
`project:awaiting-ready` or `project:awaiting-acceptance`; a story is
`story:blocked:poison` or `story:blocked:scope`; a sampling audit is waiting; you
are about to tell Mahesh that something passed, is ready, or needs approval; or
he asks whether to approve something. Do not summarise an agent's claim to him
before checking it — the summary is the thing most likely to be wrong.

When reviewing a project's criteria, an acceptance claim, a story, a pull
request, or a script you are being asked to run, follow
`.claude/skills/bell-check/SKILL.md`. It is a plain file — read it directly, no
tooling required. The short form:

**Three questions, in order.**

1. **Show me where.** Every claim needs an artifact — file path, PR number, label
   timestamp, test name. A claim whose evidence is another claim is unverified.
2. **What would have failed?** If no input could make the check report a failure,
   it is not a check.
3. **Is the claim narrower than it sounds?** Find the scope it actually covers.

**Verify against the repository, never against a summary.** Read the file. Read
the script before running it. Use
`gh api repos/OWNER/REPO/issues/N/timeline` for what labels actually did. If a
decision landed under a minute after the thing it approved, nobody read it.

**Report in plain language.** Lead with the verdict in one line. Name what is
broken and skip what is fine — two bad criteria out of sixteen means two
paragraphs. No section references unless asked. End with the decision he faces,
stated as a choice.

**Never ring the bell.** Do not post a comment beginning `## Plan approval` or
`## Acceptance`, and never write a `decision:` or `result:` field, unless he has
stated the verdict and asked for it to be posted. Those two headings are how the
runtime recognises a human decision — writing one yourself forges his signature.
Post review input under any other heading.

## He decides here; you post it there

Mahesh does not use git or the GitHub UI. He states his decision in conversation
and you record it on the issue. This is the normal path, not an exception.

**When he gives a verdict, post it.** Write the §5 comment — `## Plan approval`
with `decision: approved`, or `## Acceptance` with `result:` and one line per
criterion — quote the criteria checklist verbatim, then apply the label. Comment
first, label second, always.

**Never invent the verdict.** Post only a decision he has actually stated in this
conversation, in the words he gave. "Looks fine" is not an approval of sixteen
criteria. If you are unsure what he decided, ask — do not infer it from the fact
that he did not object.

**Say in the comment that you transcribed it.** One line naming that he gave the
decision in session and you recorded it. The factory runs on one shared
credential, so nothing downstream can tell his signature from yours; writing the
provenance down is the only thing that keeps the record honest.

**Bind correction guidance to the rejected revision.** When Mahesh supplies
human review input, requests changes on a delivery PR, or authorizes a bounded
retry, fetch the linked PR's exact 40-character head first. Put the comment on
the Story or linked PR before changing `story:in-review` to `story:ready`, keep
the transcription provenance above, and append exactly one marker:

`<!-- correction-context:v1:KIND:story:N:pr:P:head:SHA -->`

`KIND` is one of `human-review`, `request-changes`, or
`retry-authorization`; `N` is the Story number, `P` the linked PR number, and
`SHA` its exact lowercase head. Comment first, label second. A missing,
wrong-head, malformed, or untrusted marker is deliberately invisible to the
next delivery worker and the dispatcher will refuse the retry before consuming
an Attempt.

**Run bell-check before he decides, not after.** He is relying on you to have
checked. A verdict he gives on an unchecked artifact is your failure, not his.

## Explain in plain language by default

Not only when asked. He should never have to say "explain in simple terms" — if
he does, the first version was wrong.

- **Short sentences.** One idea each.
- **Lead with the answer**, then the reason. Never build up to it.
- **No section references** (`§4.1`, `P4-09`) unless he asks for them. Say what
  the rule means, not where it lives.
- **No jargon without the plain word beside it.** "Poisoned — it failed three
  times and stopped."
- **Say what it means for him**, not what the system did. "You need to rescue it"
  beats "the attempt budget is exhausted."
- **Name what is broken; skip what is fine.** Two bad items out of sixteen means
  two short paragraphs.
- **End with his choice**, stated as a choice.
- **Give every number a plain-language meaning.** Never leave an issue, Project,
  Story, PR, commit, count, duration, percentage, or measurement as a bare
  number. Put a short label or explanation beside it: “PR #589 — the closed-PR
  filter fix” and “69 tests passed — the complete poller suite.” The text must
  say what the number identifies or measures and, when relevant, why it matters.
  Do not merely spell the digits out in words. Keep required machine-readable
  lines and markers exact; put their explanation immediately before or after
  them instead of changing the protocol syntax.

A correct answer he cannot read is an answer that did not arrive. This applies to
every reply, not just reviews.

## Measuring test coverage

**Run the scripts. Do not improvise the measurement in the shell.**

That is not style advice. It was done with ad-hoc `coverage` invocations here
once. The numbers were correct and useless: nobody could reproduce them, nothing
would notice when they changed, and one attempt reported three phantom test
failures caused by stale `__pycache__`. A measurement that exists only in a
transcript is an anecdote.

There are two different questions, and they were conflated here once already.

**1. Does every requirement have a test?** Ask this one first.

```bash
python3 factory/acceptance/requirement_coverage.py
```

Reads declarations, runs nothing, needs no credential. A requirement with zero
tests is invisible to any percentage, and this is the only check that surfaces
it.

**2. How many lines do the tests execute?**

```bash
python3 factory/coverage_report.py --python <interpreter-with-coverage>
```

This repository has zero dependencies by design and `coverage.py` is not
vendored. Build a throwaway environment **outside** the repository:

```bash
python3 -m venv /tmp/factory-cov
/tmp/factory-cov/bin/pip install coverage
python3 factory/coverage_report.py --python /tmp/factory-cov/bin/python
```

Without it the script exits `3`. That is the correct outcome. Do not substitute
a cruder measurement and present it as the same figure.

Useful flags: `--check` measures twice in separate processes and diffs them,
`--json PATH` writes the machine-readable report, `--workdir PATH` keeps the
data files.

Exit codes: `0` clean, `1` tests failed, `3` no `coverage.py`, `4`
non-deterministic.

### Rules that hold regardless of engine

- **Never quote a number measured against failing tests.** The script warns and
  exits `1`. Fix the tests first — a figure from a red suite describes code that
  does not work.
- **Never gate on a coverage threshold.** Coverage is reported, never enforced.
  A required check with a coverage floor is a number the code under test can
  raise by writing shallow tests, which is the class of self-certifying control
  `factory/spec/state-schema.md` §9.14 rules out.
- **Classify new test files explicitly.** `INTEGRATION` and `ACCEPTANCE` are
  named sets inside `factory/coverage_report.py`; unit is the remainder. A file
  declared but missing is a hard error. A misfiled test moves a layer's number
  without changing a line of code.
- **The end-to-end layer is opt-in and reported apart.** `--with-e2e` writes to a
  real repository and spends real engine invocations, so it is never folded into
  the figure `--check` verifies.
- **Read the unique-contribution section as information, not a verdict.** Line
  coverage cannot see wiring. Every unit test here passed while the dependency
  defect in #107 blocked the factory for two days — the lines were covered and
  the composition was wrong. The acceptance suite exists to prove composition,
  and that contribution does not show up as points.

If the measurement needs to change, change the script. The script is the
measurement.

The fuller reasoning, including the threat each determinism control answers,
is in `.claude/skills/coverage/SKILL.md` and the `factory/coverage_report.py`
module docstring. Both are readable by any engine — they are just files.
