#!/usr/bin/env bash
# Files the Claude PR review's private findings (security flaws already on main or released) as
# private vulnerability reports, skipping any whose fingerprint an existing advisory carries.
#
# The job log is public, so nothing here prints anything about a finding, not even a count, and
# every run makes the same calls whether or not there is one.
#
# Env: EXECUTION_FILE (claude-code-action's execution_file), GH_TOKEN (the advisory App's
# token), GITHUB_REPOSITORY, PR_NUMBER, HEAD_SHA.
set -euo pipefail

findings=$(jq -c '[.[] | select(.type == "result")] | last | .structured_output.private_findings' \
  "$EXECUTION_FILE")
if [[ -z $findings || $findings == null ]]; then
  echo "::error::the review's structured output is missing from its execution file"
  exit 1
fi

known=$(mktemp)
for state in triage draft published closed; do
  gh api --paginate "repos/$GITHUB_REPOSITORY/security-advisories?state=$state&per_page=100" \
    --jq '.[].description // ""' >>"$known"
done

while read -r finding; do
  path=$(jq -r '.path' <<<"$finding")
  path=${path#./}
  path=${path#/}
  symbol=$(jq -r '.symbol | gsub("^\\s+|\\s+$"; "")' <<<"$finding")
  cwe=$(jq -r '.cwe | ascii_upcase | gsub("\\s"; "")' <<<"$finding")
  [[ $cwe =~ ^CWE-[0-9]+$ ]] || cwe=CWE-UNKNOWN
  marker="Fingerprint: \`$path::$symbol::$cwe\`"

  if grep -qF -- "$marker" "$known"; then
    continue
  fi

  jq -n --argjson f "$finding" --arg marker "$marker" --arg cwe "$cwe" \
    --arg pr "$PR_NUMBER" --arg sha "$HEAD_SHA" '
    {
      summary: $f.summary,
      description: "\($f.description)\n\n---\nReported by the Claude PR review of #\($pr) at `\($sha)`.\n\($marker)",
      severity: $f.severity,
      vulnerabilities: [{package: {ecosystem: "pip", name: "foldyard"}}]
    } + (if $cwe == "CWE-UNKNOWN" then {} else {cwe_ids: [$cwe]} end)' |
    gh api -X POST "repos/$GITHUB_REPOSITORY/security-advisories/reports" --input - \
      >/dev/null 2>&1 || {
      echo "::error::filing a private vulnerability report failed"
      exit 1
    }
  printf '%s\n' "$marker" >>"$known"
done < <(jq -c '.[]' <<<"$findings")
