#!/bin/sh
# Run the live end-to-end suite against this repository.
#
# **This creates real issues and spends real engine invocations.** The suite
# announces its cost before spending it; see `factory/acceptance/README.md`.
#
# Convenience only, on the same terms as `poll.sh`: environment and arguments,
# no policy. The worker declarations below are configuration the worker contract
# already defines (#84) — this file chooses none of the factory's behaviour, and
# `e2e.py`'s preflight rejects a declaration that could not honour the contract
# rather than trusting this wrapper to be correct.
#
#   ./live-e2e.sh --list              print the requirement map, run nothing
#   ./live-e2e.sh --only dispatch     one requirement
#   ./live-e2e.sh                     every reachable requirement
set -e
export FACTORY_WORKER_ORDER="${FACTORY_WORKER_ORDER:-claude-delivery,codex-delivery}"
export FACTORY_WORKER_CLAUDE_DELIVERY_LAUNCH="python3 factory/runtime/bridge.py --engine claude --story {story} --project {project}"
export FACTORY_WORKER_CLAUDE_DELIVERY_CAPABILITIES=delivery
export FACTORY_WORKER_CODEX_DELIVERY_LAUNCH="python3 factory/runtime/bridge.py --engine codex --story {story} --project {project}"
export FACTORY_WORKER_CODEX_DELIVERY_CAPABILITIES=delivery
GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token)}"; export GITHUB_TOKEN
exec python3 factory/acceptance/e2e.py \
  --repo "${FACTORY_REPO:-maheshhbhat/ai-software-factory}" \
  --commitment "${FACTORY_COMMITMENT:-54}" \
  --project "${FACTORY_PROJECT:-182}" "$@"
