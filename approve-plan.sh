#!/bin/sh
# Format a human's plan-approval decision to the §5.1 comment contract.
# This wrapper never decides approval: it previews the exact comment and
# requires the operator to type the literal decision before anything is posted.
set -eu

usage() {
  echo "usage: $0 PROJECT_NUMBER [OWNER/REPO]" >&2
  exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage

project_number=$1
repo=${2:-${FACTORY_REPO:-maheshhbhat/ai-software-factory}}

case "$project_number" in
  ''|*[!0-9]*) usage ;;
esac

approval_file=$(mktemp "${TMPDIR:-/tmp}/factory-plan-approval.XXXXXX")
trap 'rm -f "$approval_file"' EXIT HUP INT TERM

criteria=$(
  gh issue view "$project_number" --repo "$repo" --json body --jq '.body' |
    awk '
      /^### Falsifiable acceptance criteria$/ { capture=1; next }
      /^### Stories$/ { capture=0 }
      capture { print }
    '
)

if [ -z "$criteria" ]; then
  echo "error: issue #$project_number has no rendered Falsifiable acceptance criteria section" >&2
  exit 1
fi

actor=$(gh api user --jq '.login')

{
  printf '%s\n\n' '## Plan approval'
  printf '%s\n' 'decision: approved'
  printf 'actor: @%s\n\n' "$actor"
  printf '%s\n\n' 'Approved criteria (verbatim copy of the Falsifiable acceptance criteria section at approval time):'
  printf '%s\n' "$criteria"
} > "$approval_file"

echo "Approval comment to post to $repo#$project_number:"
echo
sed 's/^/  /' "$approval_file"
echo
printf '%s' 'Type approved to post this human decision: '
IFS= read -r confirmation

if [ "$confirmation" != "approved" ]; then
  echo "not posted" >&2
  exit 1
fi

gh issue comment "$project_number" --repo "$repo" --body-file "$approval_file"

