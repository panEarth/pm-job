#!/usr/bin/env bash
# Sloučí PR aktuální feature branch do main (GitHub Pages servíruje z main).
# PR musí existovat — vytvoří ho open_git_pr MCP nebo gh pr create.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" == "main" ]]; then
  echo "Již na main — push stačí pro GitHub Pages."
  git push origin main
  exit 0
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Chyba: necommitnuté změny. Nejdřív commitni state + docs/jobs.json." >&2
  exit 1
fi

git push -u origin "$BRANCH" 2>/dev/null || git push origin "$BRANCH"

# PR číslo: argument, env, nebo lookup podle branch
PR_NUM="${1:-${PR_NUMBER:-}}"
if [[ -z "$PR_NUM" ]]; then
  PR_NUM="$(gh pr list --head "$BRANCH" --base main --state open --json number -q '.[0].number' 2>/dev/null || true)"
fi

if [[ -z "$PR_NUM" ]]; then
  echo "PR pro branch ${BRANCH} neexistuje — zkouším gh pr create…" >&2
  DATE="$(date +%d.%m.%Y)"
  if gh pr create \
    --base main \
    --head "$BRANCH" \
    --title "PM Job Monitor — ${DATE}" \
    --body "Denní monitoring PM pozic — automatický merge pro GitHub Pages." 2>/dev/null; then
    PR_NUM="$(gh pr list --head "$BRANCH" --base main --state open --json number -q '.[0].number')"
  fi
fi

if [[ -z "$PR_NUM" ]]; then
  echo "Chyba: PR nenalezen. Vytvoř ho přes open_git_pr MCP, pak spusť:" >&2
  echo "  bash .cursor/skills/pm-job-monitor/publish.sh <číslo_PR>" >&2
  exit 1
fi

echo "Slučuji PR #${PR_NUM} (branch ${BRANCH}) do main…"
gh pr ready "$PR_NUM" 2>/dev/null || true
gh pr merge "$PR_NUM" --merge --delete-branch=false
echo "Sloučeno PR #${PR_NUM} do main."

# Počkej na GitHub Pages deploy
sleep 3
RUN_ID="$(gh run list --branch main --workflow "pages-build-deployment" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
if [[ -n "$RUN_ID" ]]; then
  gh run watch "$RUN_ID" --exit-status 2>/dev/null || echo "Varování: pages deploy ještě neproběhl."
fi

echo "Web: https://panearth.github.io/pm-job/"
