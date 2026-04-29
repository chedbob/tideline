#!/usr/bin/env bash
# Tideline deploy — push to GitHub, set Actions secrets, trigger first run.
#
# Prerequisites:
#   1. GitHub CLI 'gh' installed: https://cli.github.com
#   2. Authenticated: gh auth login
#   3. Git configured (git config user.name / user.email)
#   4. workers/.env populated with all secrets (run configure.py first)
#
# Usage:
#   bash deploy.sh
#
# Idempotent — safe to re-run. Will not duplicate the repo, will overwrite
# existing GitHub Actions secrets in place.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ------------------------------------------------------------------
# Pre-flight checks
# ------------------------------------------------------------------

REPO_NAME="${TIDELINE_REPO:-tideline}"

err() { echo "ERROR: $*" >&2; exit 1; }

command -v gh    >/dev/null || err "GitHub CLI 'gh' not installed. Install: https://cli.github.com"
command -v git   >/dev/null || err "git not installed."
command -v curl  >/dev/null || err "curl not installed."

gh auth status >/dev/null 2>&1 || err "Not authenticated. Run: gh auth login"

if ! git config user.email >/dev/null 2>&1; then
    err "git user.email not set. Run: git config --global user.email YOUR@EMAIL"
fi

if [ ! -f "workers/.env" ]; then
    err "workers/.env not found. Run: python configure.py"
fi

# Load .env into shell
set -a
source workers/.env
set +a

# Required secrets
for v in FRED_API_KEY R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET R2_PUBLIC_URL; do
    if [ -z "${!v:-}" ]; then
        err "Missing $v in workers/.env. Run: python configure.py"
    fi
done

GH_USER=$(gh api user --jq .login)
echo "[deploy] authenticated as @${GH_USER}"
echo "[deploy] target repo: ${GH_USER}/${REPO_NAME}"

# ------------------------------------------------------------------
# Git init + commit if needed
# ------------------------------------------------------------------

if [ ! -d ".git" ]; then
    echo "[deploy] initializing git repo..."
    git init -q
    git checkout -b main 2>/dev/null || git branch -M main
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "[deploy] committing pending changes..."
    git add -A
    git commit -m "configure for deploy" -q
fi

# ------------------------------------------------------------------
# Create or push to GitHub repo
# ------------------------------------------------------------------

if gh repo view "${GH_USER}/${REPO_NAME}" >/dev/null 2>&1; then
    echo "[deploy] repo exists — pushing latest to main..."
    if ! git remote get-url origin >/dev/null 2>&1; then
        git remote add origin "https://github.com/${GH_USER}/${REPO_NAME}.git"
    fi
    git push -u origin main
else
    echo "[deploy] creating public repo ${REPO_NAME}..."
    gh repo create "${REPO_NAME}" --public --source=. --remote=origin --push
fi

# ------------------------------------------------------------------
# Set Actions secrets
# ------------------------------------------------------------------

echo "[deploy] setting GitHub Actions secrets..."
set_secret() {
    local name="$1"
    local val="$2"
    if [ -z "$val" ]; then
        echo "  - $name: SKIP (empty)"
        return
    fi
    gh secret set "$name" --body "$val" >/dev/null
    echo "  - $name: ok"
}
set_secret FRED_API_KEY            "$FRED_API_KEY"
set_secret COINGECKO_DEMO_KEY      "${COINGECKO_DEMO_KEY:-}"
set_secret R2_ACCOUNT_ID           "$R2_ACCOUNT_ID"
set_secret R2_ACCESS_KEY_ID        "$R2_ACCESS_KEY_ID"
set_secret R2_SECRET_ACCESS_KEY    "$R2_SECRET_ACCESS_KEY"
set_secret R2_BUCKET               "$R2_BUCKET"

# ------------------------------------------------------------------
# Trigger workflow + watch
# ------------------------------------------------------------------

WORKFLOW="Refresh Tideline data"
echo "[deploy] triggering workflow: $WORKFLOW"
gh workflow run "$WORKFLOW"

echo "[deploy] waiting 5s for run to register..."
sleep 5

RUN_ID=$(gh run list --workflow "$WORKFLOW" --limit 1 --json databaseId --jq '.[0].databaseId' || true)
if [ -n "$RUN_ID" ]; then
    echo "[deploy] watching run $RUN_ID (Ctrl-C to detach; cron will keep running)..."
    gh run watch "$RUN_ID" --exit-status || echo "[deploy] WARN: workflow run reported failure — check 'gh run view $RUN_ID --log' for details"
fi

# ------------------------------------------------------------------
# Verify R2 publication
# ------------------------------------------------------------------

echo ""
echo "[deploy] verifying R2 public URL..."
JSON_URL="${R2_PUBLIC_URL}/latest.json"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$JSON_URL" || echo "000")
if [ "$HTTP" = "200" ]; then
    echo "  OK: $JSON_URL returns 200"
    SIZE=$(curl -s -o /dev/null -w "%{size_download}" "$JSON_URL")
    echo "  size: $SIZE bytes"
else
    echo "  WARN: $JSON_URL returned $HTTP"
    echo "  - if 403/404: R2 bucket public access not enabled, or this is the first run and upload hasn't happened yet"
    echo "  - if 0/000: network or DNS issue"
fi

# ------------------------------------------------------------------
# Final guidance
# ------------------------------------------------------------------

cat <<EOF

============================================================
Deploy steps that REMAIN (browser actions, can't be automated):
------------------------------------------------------------
1. Cloudflare → Workers & Pages → Create → Pages → Connect to Git
   - Authorize ${GH_USER}/${REPO_NAME}
   - Build command: leave blank
   - Build output directory: web
   - Save and Deploy

2. Once Pages deploy is live, visit your Pages URL and verify:
   - Dashboard loads, Zone 0/1/2 populated
   - Methodology page loads, research log renders at the bottom

3. Validate X share preview: https://cards-dev.twitter.com/validator
   Enter your Pages URL → expect to see the OG card.

4. Tweet + pin.

If R2 fetch shows CORS errors in the browser console, add this CORS rule
to your bucket (Cloudflare R2 → bucket → Settings → CORS Policy):

[
  {
    "AllowedOrigins": ["${SITE_URL:-https://your-pages-url}"],
    "AllowedMethods": ["GET"],
    "AllowedHeaders": ["*"]
  }
]
============================================================
EOF
