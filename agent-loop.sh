#!/usr/bin/env bash
# Executes command files dropped into .agent/queue/*.sh (one at a time, oldest first) and writes
# stdout+stderr to .agent/done/<name>.out. Lets Claude run tests on this Mac. Ctrl-C to stop.
cd "$(dirname "$0")"; mkdir -p .agent/queue .agent/done
export PATH="$HOME/Library/Android/sdk/platform-tools:$HOME/Library/Android/sdk/emulator:$PATH"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
echo "agent loop running in $PWD — watching .agent/queue — Ctrl-C to stop"
while true; do
  for f in $(ls .agent/queue/*.sh 2>/dev/null | sort); do
    age=$(( $(date +%s) - $(stat -f %m "$f") ))
    [ "$age" -ge 2 ] || continue            # let the file finish syncing
    n=$(basename "$f" .sh)
    echo "▶ $n  ($(date +%H:%M:%S))"
    ( bash "$f" ) > ".agent/done/$n.out" 2>&1; rc=$?
    echo "exit=$rc" >> ".agent/done/$n.out"
    mv "$f" ".agent/done/$n.sh"
    echo "✓ $n  exit=$rc  ($(wc -l < .agent/done/$n.out | tr -d ' ') lines)"
  done
  sleep 1
done
