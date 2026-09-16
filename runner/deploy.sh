#!/usr/bin/env bash
# Copies build/dbq.jar into the app's sandbox (code_cache/dbq.jar) and runs a smoke query.
#   ./deploy.sh <package> [db-file] [serial]
# Mode is chosen by ANDROID_DB_RUNNER: "run-as" (default, debuggable app) or "root" (adb root, any app).
set -euo pipefail
cd "$(dirname "$0")"

PKG="${1:?package name}"
DB="${2:-}"
SERIAL="${3:-${ANDROID_SERIAL:-}}"
RUNNER="${ANDROID_DB_RUNNER:-run-as}"
ADB=("${ADB:-adb}")
[ -n "$SERIAL" ] && ADB+=(-s "$SERIAL")
JAR="${ANDROID_DB_JAR:-build/dbq.jar}"
[ -f "$JAR" ] || { echo "no $JAR — run ./build.sh first" >&2; exit 1; }

"${ADB[@]}" push "$JAR" /data/local/tmp/dbq.jar >/dev/null

if [ "$RUNNER" = "root" ]; then
  "${ADB[@]}" root >/dev/null || true
  sleep 1
  echo "runner: root — jar stays in /data/local/tmp/dbq.jar"
  PREFIX="CLASSPATH=/data/local/tmp/dbq.jar app_process / dbq.Main"
  BASE="/data/data/$PKG"
else
  # /data/local/tmp is shell:shell 0771, so the app uid cannot read it directly: stream it across.
  "${ADB[@]}" shell "cat /data/local/tmp/dbq.jar | run-as $PKG sh -c 'rm -f code_cache/dbq.jar; cat > code_cache/dbq.jar && chmod 444 code_cache/dbq.jar'"
  echo "runner: run-as — deployed to $("${ADB[@]}" shell run-as "$PKG" ls -l code_cache/dbq.jar)"
  PREFIX="run-as $PKG sh -c 'CLASSPATH=code_cache/dbq.jar app_process / dbq.Main"
  BASE="."
fi

if [ -z "$DB" ]; then
  DB="$("${ADB[@]}" shell "run-as $PKG ls databases 2>/dev/null || ls /data/data/$PKG/databases" 2>/dev/null | grep -v -- '-journal\|-wal\|-shm' | head -1 || true)"
fi
if [ -n "$DB" ]; then
  REQ=$(printf '{"db":"%s/databases/%s","sql":"SELECT name FROM sqlite_master WHERE type=\x27table\x27","mode":"read","limit":50}' "$BASE" "$DB" | base64 | tr -d '\n')
  echo "smoke query on $DB:"
  if [ "$RUNNER" = "root" ]; then
    "${ADB[@]}" exec-out "$PREFIX $REQ 2>/dev/null"; echo
  else
    "${ADB[@]}" exec-out "$PREFIX $REQ 2>/dev/null'"; echo
  fi
else
  echo "no database found to smoke-test; deploy done."
fi
