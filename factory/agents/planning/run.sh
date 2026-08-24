#!/bin/sh
# Dispatcher-facing planning wrapper: artifact identity only.
set -eu

# Planning and review use the same dedicated Claude identity.  Normal polling
# loads this credential before it launches either engine, but operators also
# invoke this wrapper directly when creating the first Project from a roadmap
# commitment.  Keep that direct path equally self-contained.  A credential
# supplied by the caller takes precedence; with neither source, invoke.py still
# starts and reports the engine's own authentication failure.
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -r "$HOME/.factory-reviewer-token" ]; then
  CLAUDE_CODE_OAUTH_TOKEN=$(cat "$HOME/.factory-reviewer-token")
  export CLAUDE_CODE_OAUTH_TOKEN
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/invoke.py" --repo "$1" --artifact "$2"
