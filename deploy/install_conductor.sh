#!/bin/bash
# Install the conductor (the shared-chat service) as a KeepAlive launchd agent.
# Operator runs this once: ! bash ~/calwbot/navigator/deploy/install_conductor.sh
set -euo pipefail
NAV=/Users/Manoj/calwbot/navigator
cd "$NAV"
plutil -lint deploy/launchd/com.calwbot.conductor.plist
cp deploy/launchd/com.calwbot.conductor.plist ~/Library/LaunchAgents/
for KEY in NAVIGATOR_ACT_BASE_URL NAVIGATOR_REPOS_FILE; do
  if ! plutil -extract "EnvironmentVariables.$KEY" raw ~/Library/LaunchAgents/com.calwbot.conductor.plist >/dev/null 2>&1; then
    echo "WARNING: installed plist lacks $KEY - builds are off / nothing auto-approves" >&2
  fi
done
launchctl bootout "gui/$(id -u)/com.calwbot.conductor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.calwbot.conductor.plist
sleep 3
ls -l /Users/Manoj/calwbot/logs/conductor/conductor.sock
curl -s --unix-socket /Users/Manoj/calwbot/logs/conductor/conductor.sock http://c/status; echo
echo "conductor installed: one shared conversation on logs/conductor/conductor.sock (0600)"
