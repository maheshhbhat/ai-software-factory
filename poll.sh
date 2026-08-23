#!/bin/sh
# Run the factory poller against this repository.
#
# Convenience only: this wrapper supplies environment and arguments and holds no
# policy of its own. Authorization, eligibility, WIP, lifecycle and recovery all
# live in GitHub (§9.12) and in the modules `poller.py` invokes. If a rule ever
# appears in this file, it has been put in the wrong place — there would then be
# two sources for it, and only one of them is the system of record.
#
# Configuration is a different thing from policy, and it *is* this file's
# business: which engine services a delivery, and how it is invoked (#84). The
# declarations below used to exist only in `live-e2e.sh`, so whether a worker
# existed at all depended on which wrapper started the poller. A poller started
# without them claims Stories it has no engine to service — the claim expires,
# the Story returns to `story:ready`, and the attempt budget drains. Stories
# #299 and #300 poisoned exactly that way on 2026-08-22, five and six claims
# each, no branch, no pull request, no worker process ever started. So the
# declarations live here, and this wrapper refuses to poll without them. That
# refusal decides nothing about what may run; it only says whether this wrapper
# is configured to run anything at all.
#
# The launch target is the Phase 4 delivery worker, `factory/agents/worker/
# invoke.py`, and deliberately not `factory/runtime/bridge.py`. The bridge is
# the Phase 2 acknowledgement stub and its prompt instructs the engine not to
# create branches, commits or pull requests, so every Story routed to it reaches
# `story:completed` having produced nothing — #299, #300, #303, #318 and #324.
#
# Every value below stays overridable from the caller's environment, but each is
# defaulted with a plain conditional rather than `${VAR:-default}`. The launch
# strings contain `{story}`, and an unquoted `}` closes a parameter expansion
# early: `${X:-python3 … --story {story} --project {project}}` resolves to
# `--story {story` plus a stray `}`, and the engine exits 2 on every dispatch.
#
#   ./poll.sh --once              one cycle and exit
#   ./poll.sh --once --dry-run    decide, write nothing
#   ./poll.sh                     watch continuously (this is the service)
set -e

if [ -z "$FACTORY_REPO" ]; then
  FACTORY_REPO="maheshhbhat/ai-software-factory"
fi
if [ -z "$FACTORY_COMMITMENT" ]; then
  FACTORY_COMMITMENT="54"
fi
export FACTORY_REPO FACTORY_COMMITMENT

# The reviewer runs from the wrapper, not from a shell the operator remembers to
# export by hand. Phase 4 review is how findings reach a delivered pull request;
# a poller that dispatches work and never reviews it leaves every delivery
# waiting on a person who was not told.
if [ -z "$FACTORY_PHASE4_REVIEWS" ]; then
  FACTORY_PHASE4_REVIEWS="1"
fi
export FACTORY_PHASE4_REVIEWS

# Preference order, and the engines it names. `codex-delivery` is left in the
# order but undeclared here: `invoke.py` has no engine selector, and pointing a
# second worker name at the same command would put "codex ran" in the audit
# trail when Claude did. Declare `FACTORY_WORKER_CODEX_DELIVERY_LAUNCH` from the
# caller's environment to fill the slot.
if [ -z "$FACTORY_WORKER_ORDER" ]; then
  FACTORY_WORKER_ORDER="claude-delivery,codex-delivery"
fi
export FACTORY_WORKER_ORDER

# `invoke.py` takes `--repo` and `--story` and nothing else; it reads the Story
# from the substrate itself, including the `### Spend cap` that bounds it. It is
# not given `--project`, which it does not accept and would exit 2 on.
if [ -z "$FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH" ]; then
  FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH="python3 factory/agents/worker/invoke.py --repo $FACTORY_REPO --story {story}"
fi
if [ -z "$FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES" ]; then
  FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES="delivery"
fi
if [ -z "$FACTORY_WORKER_CODEX_DELIVERY_CAPABILITIES" ]; then
  FACTORY_WORKER_CODEX_DELIVERY_CAPABILITIES="delivery"
fi
export FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES
export FACTORY_WORKER_CODEX_DELIVERY_CAPABILITIES

# Resolve the order the same way `workers.configured_workers()` does, so what
# this wrapper checks is what the dispatcher will find. A name carrying no
# `_LAUNCH` declaration resolves to nothing there and is skipped in silence.
resolved=""
for worker in $(printf '%s' "$FACTORY_WORKER_ORDER" | tr ',' ' '); do
  # Anything outside the Agent-ID alphabet is not a worker name and is never
  # expanded — this loop reads variables by computed name.
  case "$worker" in
    *[!a-z0-9-]*) continue ;;
  esac
  key=$(printf '%s' "$worker" | tr 'a-z-' 'A-Z_')
  eval "declared=\${FACTORY_WORKER_${key}_LAUNCH:-}"
  if [ -n "$declared" ]; then
    resolved="$resolved $worker"
  fi
done

if [ -z "$resolved" ]; then
  echo "poll.sh: refusing to poll: no delivery worker resolves." >&2
  echo "  FACTORY_WORKER_ORDER=$FACTORY_WORKER_ORDER" >&2
  echo "  none of those names carries a FACTORY_WORKER_<NAME>_LAUNCH declaration," >&2
  echo "  so workers.configured_workers() would return an empty list." >&2
  echo "Polling in that state claims Stories nothing can service: the claim expires," >&2
  echo "the Story returns to story:ready, and its attempt budget drains (#299, #300)." >&2
  exit 2
fi
echo "[poll.sh] delivery workers:$resolved" >&2

# Two bounds this wrapper deliberately does not set, because setting them here
# would create a second source for a decision the runtime owns:
#
#   * how long a launched worker may run before the poller gives up on it
#     (`workers.LAUNCH_TIMEOUT_SECONDS`), and
#   * whether a Story woken again in a later cycle is redispatched
#     (the `seen` guard in `poller.py`).
#
# Both are wrong for delivery work today, and both are being fixed where they
# live: the launch cap in #345 (a fixed 60s that bounded the Phase 2
# acknowledgement bridge — a single comment — and kills a real delivery
# mid-flight; the bound belongs to the claimed Story's `### Spend cap`), and the
# skip guard in #342 (built once for the life of the process instead of once per
# cycle, so a Story returned to `story:ready` by review findings is skipped
# forever — #328 on 2026-08-23: no `worker.launch` event, head unchanged, while
# the reviewer re-read identical code). Neither is reachable from a wrapper —
# there is no environment hook for either, and inventing one would put the bound
# in two places. The tests pin that this file is not where either comes from, so
# neither fix lands here by mistake and then again in its own Story.

if [ -z "$GH_TOKEN" ]; then
  GH_TOKEN=$(gh auth token)
fi
export GH_TOKEN

exec python3 factory/runtime/poller.py \
  --repo "$FACTORY_REPO" \
  --commitment "$FACTORY_COMMITMENT" "$@"
