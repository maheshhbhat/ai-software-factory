#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
[ "$#" -eq 2 ] || { echo "usage: $0 OWNER/REPO STORY" >&2; exit 2; }
exec python3 "$here/invoke.py" --repo "$1" --story "$2"
