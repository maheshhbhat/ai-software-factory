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
  [--file factory/touchlog/touchlog.jsonl] [--timestamp 2026-08-19T14:03:00Z]
```

* Validates enums, rejects `seconds_spent < 0`, rejects any `--timestamp` that is not ISO-8601 UTC, and ensures the existing file is valid JSONL before appending.
* No dependency on shell `flock(1)`. Uses Python stdlib advisory lock (`fcntl` on Unix, `msvcrt` on Windows) where available; otherwise relies on `O_APPEND` atomicity for small writes.
* Exactly one line per invocation; never rewrites the file.

## Invariants

* Append-only: no edits, no deletions. History is `git log -- factory/touchlog/touchlog.jsonl`.
* One JSON object per line; `ensure_ascii=False`, separators `(",", ":")` (canonical via helper, but any valid JSON that parses is accepted).
