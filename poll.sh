#!/bin/sh
# Run the factory poller against this repository.
#
# Convenience only: this wrapper supplies environment and arguments and holds no
# policy of its own. Authorization, eligibility, WIP, lifecycle and recovery all
# live in GitHub (§9.12) and in the modules `poller.py` invokes. If a rule ever
# appears in this file, it has been put in the wrong place — there would then be
# two sources for it, and only one of them is the system of record.
#
#   ./poll.sh --once              one cycle and exit
#   ./poll.sh --once --dry-run    decide, write nothing
#   ./poll.sh                     watch continuously (this is the service)
set -e
exec env GH_TOKEN="${GH_TOKEN:-$(gh auth token)}" python3 factory/runtime/poller.py \
  --repo "${FACTORY_REPO:-maheshhbhat/ai-software-factory}" \
  --commitment "${FACTORY_COMMITMENT:-54}" "$@"
