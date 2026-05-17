#!/bin/bash
#===============================================================================
# Deploy Dashboard
#===============================================================================
# Pushes the generated dashboard.html to the gh-pages branch.
# Cloudflare Pages (or GitHub Pages) watches this branch and auto-deploys.
#
# Usage:
#   ./scripts/deploy-dashboard.sh                    # default: data/output/dashboard.html
#   ./scripts/deploy-dashboard.sh /path/to/dashboard.html
#
# Prerequisites:
#   - git configured with push access to origin
#   - Dashboard file must exist (run the pipeline first)
#
# Called automatically at the end of the pipeline via main.py,
# or manually after a test run.
#===============================================================================

set -euo pipefail

DASHBOARD="${1:-data/output/dashboard.html}"
BRANCH="gh-pages"
DEPLOY_DIR=$(mktemp -d)
REPO_URL=$(git config --get remote.origin.url)
TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")

log() { echo "[deploy] $1"; }

# Validate dashboard exists and isn't empty
if [ ! -f "$DASHBOARD" ]; then
    log "ERROR: Dashboard not found at $DASHBOARD"
    log "Run the pipeline first: python -m src.main"
    exit 1
fi

if [ ! -s "$DASHBOARD" ]; then
    log "ERROR: Dashboard file is empty"
    exit 1
fi

log "Deploying dashboard to $BRANCH branch..."

# Clone just the gh-pages branch (or init if it doesn't exist)
if git ls-remote --exit-code --heads origin "$BRANCH" > /dev/null 2>&1; then
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$DEPLOY_DIR"
else
    log "Creating $BRANCH branch..."
    git init "$DEPLOY_DIR"
    cd "$DEPLOY_DIR"
    git checkout -b "$BRANCH"
    git remote add origin "$REPO_URL"
    cd - > /dev/null
fi

# Copy dashboard and commit
cp "$DASHBOARD" "$DEPLOY_DIR/index.html"

# Copy Pages Functions if present (proxies /api/* to bound Workers)
if [ -d "functions" ]; then
    cp -r functions "$DEPLOY_DIR/functions"
fi

cd "$DEPLOY_DIR"

# Add a minimal robots.txt (authenticated anyway, but good practice)
cat > robots.txt << 'EOF'
User-agent: *
Disallow: /
EOF

git add -A

# Only commit + push if there are changes
if git diff --cached --quiet 2>/dev/null; then
    log "No changes to deploy (dashboard unchanged)"
else
    git commit -m "deploy: dashboard update $TIMESTAMP"
    git push origin "$BRANCH"
    log "Dashboard deployed to $BRANCH branch"
fi

# Cleanup
cd - > /dev/null
rm -rf "$DEPLOY_DIR"

log "Done"
