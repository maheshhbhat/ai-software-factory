# Touch log — `factory/touchlog/touchlog.jsonl`

Append-only log of every human bell. Used for KPIs (`relay` must trend to 0, `decision|audit|rescue` are expected). Ref: `factory/spec/state-schema.md` §6, `implementation-plan-v1.md` Phase 1.

This log is **measurement evidence only**. The decision itself is recorded as a comment on the affected issue per `state-schema.md` §5 — every bell produces both a comment and exactly one line here.

## File

* Path: `factory/touchlog/touchlog.jsonl`
* Format: one JSON object per line (JSONL), UTF-8, `\n` terminated. No header, no comment line, no empty lines. The file starts empty.
* Validating: every non-empty line must parse as JSON (`jq -e .` or `python -m json.tool`).

## Schema (per line)

```json
{
  "timestamp": "2026-08-19T14:03:00Z",
  "project": "#12",
  "story": "#15",
  "bell_type": "plan-approval",
  "classification": "decision",
  "seconds_spent": 180,
  "note": "approved criteria",
  "actor": "@alice"
}
```

| Field | Type | Required | Values |
|---|---|---|---|
| `timestamp` | string | yes | ISO-8601 UTC, trailing `Z` or `+00:00`; `append.py` defaults to now if omitted and rejects naive, non-UTC, or unparseable values |
| `project` | string | yes | Project issue ref, e.g. `"#12"` |
| `story` | string \| null | yes (null allowed) | Story issue ref or `null` for project-level bells |
| `bell_type` | string | yes | `plan-approval` \| `hazard-ack` \| `poison-rescue` \| `scope-decision` \| `cutover-approval` \| `acceptance` \| `sampling` |
| `classification` | string | yes | `decision` \| `audit` \| `rescue` \| `relay` (exactly these four) |
| `seconds_spent` | integer | yes | `>=0` |
| `note` | string | yes (may be `""`) | Human-readable context |
| `actor` | string | yes (may be `""`) | Handle, e.g. `"@alice"` |

`project`/`story` refs are free-form strings but should be `#N` or `owner/repo#N`. No separate workflow database — the structured fields live in issue bodies (`state-schema.md` §3), decisions live in issue comments (§5), and this log is the measurement substrate (§6).

## Helper

```
python factory/touchlog/append.py \
  --project "#12" --bell-type plan-approval --classification decision \
  --seconds-spent 180 [--story "#15"] [--note "approved criteria"] [--actor "@you"] \
  [--file factory/touchlog/touchlog.jsonl] [--timestamp 2026-08-19T14:03:00Z] \
  [--unique-note-marker "stable-idempotency-key"]
```

* Validates enums, rejects `seconds_spent < 0`, rejects any `--timestamp` that is not ISO-8601 UTC, and ensures the existing file is valid JSONL before appending.
* No dependency on shell `flock(1)`. Uses Python stdlib advisory lock (`fcntl` on Unix, `msvcrt` on Windows) where available; otherwise relies on `O_APPEND` atomicity for small writes.
* Without `--unique-note-marker`, exactly one line per successful invocation.
  With it, validation, marker lookup, and append happen under one file lock:
  zero matches appends, one match is a replay-safe no-op, and multiple matches
  fail closed. The marker must also appear in `--note` so read-back can verify it.
  The helper never rewrites the file.

## Invariants

* Append-only: no edits, no deletions. History is `git log -- factory/touchlog/touchlog.jsonl`.
* One JSON object per line; `ensure_ascii=False`, separators `(",", ":")` (canonical via helper, but any valid JSON that parses is accepted).

## Temporary Project #294 AT-07 sequencing limitation

Owner approval is recorded on Project #294. This limitation exists only for the
merge ordering between Stories #295 and #296:

* **Why the live proof is temporarily unreachable:** the controlled GitHub test
  must exercise the acceptance-touch implementation from merged `main`; #295 is
  the change that introduces that implementation, so the proof cannot run
  against merged code before #295 lands.
* **Substitute evidence for #295:** hermetic acceptance tests exercise the real
  `append.py` subprocess against isolated files, including exactly-once replay,
  changed decisions, corrupt and duplicate evidence, append/read-back failures,
  trusted-author handling, and state-transition ordering. The committed ledger
  regression also verifies the required historical entries.
* **Residual risk:** repository permissions, GitHub comment parsing, and live
  Project label mutation may behave differently from the hermetic fixtures.
* **Bound and expiry:** dependent Story #296 must run the controlled live GitHub
  acceptance transition and replay from merged `main`. Project #294 cannot be
  accepted, and Phase 5 cannot start, until that evidence is committed and #296
  is merged. This limitation expires when #296 supplies that proof.

## Phase 2 Closeout (Project #109) — the touches, and one relay

Four bells, and the relay count is the number worth reading. `relay` is the only
classification the architecture expects to trend to zero; `decision`, `audit` and
`rescue` are human judgment the factory is not trying to remove.

| When | Bell | Class | Note |
|---|---|---|---|
| 10:59 | `plan-approval` #109 | decision | the closeout plan |
| 12:49 | `hazard-ack` #113 | decision | PR #119, the gate runner |
| 12:55 | `plan-approval` #109 | **relay** | see below |
| 14:39 | `hazard-ack` #114 | decision | PR #120, `factory/spec/**` |

**relay = 1, at zero human seconds.** The CTO's approval on #109 was recorded as
prose rather than in §5.1's machine-readable shape, so the continuation pass
refused it — correctly, since reading prose to decide whether the factory is
authorized is what §9.9 forbids — and the label was moved by hand.

It is logged as `relay` rather than `decision` because no new judgment was
exercised: a decision that already existed was carried across a format gap. The
`seconds_spent` is 0 because the cost fell on a machine step, not on the human;
recording their approval time here as well would double-count the
`plan-approval` decision touch immediately above it.

The cause is fixed. #122 made every human-queue entry name the exact recordable
form, and pinned each promised literal against the parser that will read the
reply — because the failure mode is drift between an instruction and its
consumer, which a fixed-string test would not catch.

**The seconds are estimates.** Every figure above was estimated by the agent and
is flagged as such in its own `note`. Only the CTO knows what these actually
cost, and a fabricated number in a measurement log is worse than an absent one —
the whole point of this file is that it is the substrate for measurement. They
should be corrected in place when the real figures are known.
