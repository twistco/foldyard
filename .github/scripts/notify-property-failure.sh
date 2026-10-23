#!/usr/bin/env bash
# Tells the operator the weekly property run failed, as a PUBLIC issue (the run's log, which holds
# the same falsifying examples, is public already): a comment on the open issue carrying
# ISSUE_LABEL if there is one, else a new issue assigned to NOTIFY_LOGIN. Both @-mention the login. The assignment
# and the mention each email under GitHub's default settings, whoever last edited the cron line —
# GitHub's own failure email goes to that person, not to the operator.
#
# The summary (tests/tools/property_failures.py) is untrusted text: it is only ever read from its
# file into gh's --body-file, never expanded into a command line.
#
# Env: SUMMARY_FILE, NOTIFY_LOGIN, ISSUE_LABEL, DRY_RUN ("true" prints what it would post and posts
# nothing), GH_TOKEN (issues: write), GITHUB_REPOSITORY.
set -euo pipefail

: "${SUMMARY_FILE:?}" "${NOTIFY_LOGIN:?}" "${ISSUE_LABEL:?}" "${GITHUB_REPOSITORY:?}"
if [[ ! $NOTIFY_LOGIN =~ ^[A-Za-z0-9-]+$ ]]; then
  echo "::error::NOTIFY_LOGIN is not a GitHub login: $NOTIFY_LOGIN"
  exit 1
fi
if [[ ! -s $SUMMARY_FILE ]]; then
  echo "::error::no failure summary at $SUMMARY_FILE"
  exit 1
fi

title="Randomised property tests failed"
body=$(mktemp)
{
  printf '@%s: the randomised property run found a failure.\n\n' "$NOTIFY_LOGIN"
  cat "$SUMMARY_FILE"
} >"$body"

# Read-only, so the dry run asks too: it says which of the two it would do. A label that doesn't
# exist yet lists nothing (exit 0); an API error fails the step rather than open a duplicate.
existing=$(gh issue list -R "$GITHUB_REPOSITORY" --label "$ISSUE_LABEL" --state open \
  --json number --jq '.[0].number // empty')

if [[ ${DRY_RUN:-false} == true ]]; then
  if [[ -n $existing ]]; then
    echo "dry run: would comment on #$existing (open, labelled $ISSUE_LABEL) with:"
  else
    echo "dry run: would create label $ISSUE_LABEL if missing, then open an issue assigned to" \
      "$NOTIFY_LOGIN, titled: $title"
    echo "with the body:"
  fi
  echo "----------------------------------------------------------------------"
  cat "$body"
  echo "----------------------------------------------------------------------"
  exit 0
fi

if [[ -n $existing ]]; then
  gh issue comment "$existing" -R "$GITHUB_REPOSITORY" --body-file "$body"
  exit 0
fi

if ! gh label list -R "$GITHUB_REPOSITORY" --search "$ISSUE_LABEL" --json name --jq '.[].name' |
  grep -qxF -- "$ISSUE_LABEL"; then
  gh label create "$ISSUE_LABEL" -R "$GITHUB_REPOSITORY" --color d93f0b \
    --description "A failure the weekly randomised property run found"
fi
gh issue create -R "$GITHUB_REPOSITORY" --title "$title" --label "$ISSUE_LABEL" \
  --assignee "$NOTIFY_LOGIN" --body-file "$body"
