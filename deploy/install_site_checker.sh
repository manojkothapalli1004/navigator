#!/bin/bash
# Install the site checker schedule (operator runs this once: ! bash deploy/install_site_checker.sh).
# Builds its own venv (.venv-checker, never the daemon's .venv), copies the launchd unit, loads it.
set -euo pipefail
NAV=/Users/Manoj/calwbot/navigator
cd "$NAV"
UV_PROJECT_ENVIRONMENT="$NAV/.venv-checker" uv sync --frozen --extra sdk --extra checker
mkdir -p /Users/Manoj/calwbot/logs/navigator/checker
plutil -lint deploy/launchd/com.calwbot.site-checker.plist
cp deploy/launchd/com.calwbot.site-checker.plist ~/Library/LaunchAgents/
launchctl bootout "gui/$(id -u)/com.calwbot.site-checker" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.calwbot.site-checker.plist
echo "site checker installed: runs after each ACT main change and daily 07:00 (report-only)"
