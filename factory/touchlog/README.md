# Touch log — `factory/touchlog/touchlog.jsonl`

Append-only log of every human bell. Used for KPIs (`relay` must trend to 0, `decision|audit|rescue` are expected). Ref: `factory/spec/state-schema.md` §5, `implementation-plan-v1.md` Phase 1.

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
| `timestamp` | string | yes | ISO-8601 UTC; `append.py` defaults to now if omitted |
| `project` | string | yes | Project issue ref, e.g. `"#12"` |
| `story` | string \| null | yes (null allowed) | Story issue ref or `null` for project-level bells |
| `bell_type` | string | yes | `plan-approval` \| `hazard-ack` \| `poison-rescue` \| `cutover-approval` \| `acceptance` \| `sampling` |
| `classification` | string | yes | `decision` \| `audit` \| `rescue` \| `relay` (exactly these four) |
| `seconds_spent` | integer | yes | `>=0` |
| `note` | string | yes (may be `""`) | Human-readable context |
| `actor` | string | yes (may be `""`) | Handle, e.g. `"@alice"` |

`project`/`story` refs are free-form strings but should be `#N` or `owner/repo#N`. No separate workflow database — the structured fields live in issue bodies; this log is the measurement substrate.

## Helper

```
python factory/touchlog/append.py \
  --project "#12" --bell-type plan-approval --classification decision \
  --seconds-spent 180 [--story "#15"] [--note "approved criteria"] [--actor "@you"] \
  [--file factory/touchlog/touchlog.jsonl] [--timestamp 2026-08-19T14:03:00Z]
```

* Validates enums, rejects `seconds_spent < 0`, ensures existing file is valid JSONL before appending.
* No dependency on shell `flock(1)`. Uses Python stdlib advisory lock (`fcntl` on Unix, `msvcrt` on Windows) where available; otherwise relies on `O_APPEND` atomicity for small writes.
* Exactly one line per invocation; never rewrites the file.

## Invariants

* Append-only: no edits, no deletions. History is `git log -- factory/touchlog/touchlog.jsonl`.
* One JSON object per line; `ensure_ascii=False`, separators `(",", ":")` (canonical via helper, but any valid JSON that parses is accepted).
