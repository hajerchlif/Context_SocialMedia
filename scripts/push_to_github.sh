#!/usr/bin/env bash
# Push this folder to https://github.com/hajerchlif/Context_SocialMedia
# Run it from the unzipped folder, on your PC or your server.
# Authentication: GitHub username + a Personal Access Token (Settings > Developer settings > Tokens)
set -euo pipefail
cd "$(dirname "$0")/.."
git init -q 2>/dev/null || true
git checkout -B main
git add -A
git commit -m "Collector scaffold: Docker, deploy script, Claude prompt" || echo "(nothing new to commit)"
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/hajerchlif/Context_SocialMedia.git
# If the GitHub repo already has commits (e.g. a README), merge them first:
git pull --rebase origin main 2>/dev/null || true
git push -u origin main
