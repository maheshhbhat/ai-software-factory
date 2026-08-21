#!/bin/sh
# Dispatcher-facing planning wrapper: artifact identity only.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/invoke.py" --repo "$1" --artifact "$2"

