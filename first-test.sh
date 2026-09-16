#!/usr/bin/env bash
# First end-to-end test of android-db-mcp against field-service-os on an emulator.
# Everything is logged to first-test.log next to this script so it can be read back.
#   bash ~/projects/android-db-mcp/first-test.sh
set -uo pipefail
cd "$(dirname "$0")"
LOG="$PWD/first-test.log"; : > "$LOG"
exec > >(tee -a "$LOG") 2>&1

PKG="${PKG:-com.osapiens.operations.serviceos}"
ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
step() { echo; echo "=== $1 ==="; }
fail() { echo "FAIL: $1"; echo "(log: $LOG)"; exit 1; }

step "0. toolchain"
echo "ANDROID_HOME=$ANDROID_HOME"
command -v adb   >/dev/null || fail "adb not found"
command -v javac >/dev/null || fail "javac not found (need a JDK; Android Studio's is at /Applications/Android Studio.app/Contents/jbr/Contents/Home/bin)"
adb version | head -1; javac -version 2>&1
ls -d "$ANDROID_HOME"/platforms/android-* 2>/dev/null | tail -2 || fail "no platforms in SDK"
ls -d "$ANDROID_HOME"/build-tools/* 2>/dev/null | tail -2 || fail "no build-tools in SDK"

step "1. device"
adb start-server >/dev/null 2>&1
if ! adb devices | grep -qw device; then
  echo "no device online; AVDs:"; emulator -list-avds
  AVD="${AVD:-$(emulator -list-avds | head -1)}"
  [ -n "$AVD" ] || fail "no AVD exists — create one in Android Studio > Device Manager"
  echo "starting $AVD ..."
  nohup emulator -avd "$AVD" >/dev/null 2>&1 &
  adb wait-for-device
  for i in $(seq 1 90); do
    [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break; sleep 2
  done
fi
adb devices -l
SERIAL="$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')"
[ -n "$SERIAL" ] || fail "no device reached 'device' state"
export ANDROID_SERIAL="$SERIAL"
echo "using $SERIAL  (android $(adb shell getprop ro.build.version.release | tr -d '\r'), sdk $(adb shell getprop ro.build.version.sdk | tr -d '\r'), $(adb shell getprop ro.build.type | tr -d '\r') build, selinux: $(adb shell getenforce 2>/dev/null | tr -d '\r'))"

step "2. app $PKG"
if ! adb shell pm path "$PKG" | grep -q package; then
  echo "app not installed on $SERIAL."
  echo "install a debug build first, e.g.:  cd ~/field-service-os && yarn android   (or: cd android && ./gradlew installDebug)"
  fail "app not installed"
fi
adb shell pm path "$PKG" | tr -d '\r'
DEBUGGABLE="$(adb shell dumpsys package "$PKG" | grep -m1 -o 'DEBUGGABLE' || true)"
echo "flags contain DEBUGGABLE: ${DEBUGGABLE:-no}"
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 && echo "launched app" || echo "could not launch app (fine if it is already running)"
sleep 3

step "3. run-as"
RUNNER=run-as
if adb shell run-as "$PKG" id 2>&1 | tee /dev/stderr | grep -q "uid=1"; then
  echo "run-as works"
else
  echo "run-as refused → will use root mode (emulator)"
  RUNNER=root
  adb root >/dev/null 2>&1; sleep 2; adb wait-for-device
fi
export ANDROID_DB_RUNNER="$RUNNER"
if [ "$RUNNER" = root ]; then adb shell ls -l "/data/data/$PKG/databases"; else adb shell run-as "$PKG" ls -l databases; fi \
  || fail "no databases/ dir yet — open the app once so it creates its DB (AsyncStorage → RKStorage), then rerun"

step "4. build dbq.jar"
( cd runner && bash ./build.sh ) || fail "build.sh failed"

step "5. deploy + smoke query (runner=$RUNNER)"
( cd runner && bash ./deploy.sh "$PKG" ) || fail "deploy.sh failed"
echo
echo "raw app_process call WITH stderr (ART warnings are normal; look for 'denied' / 'Permission'):"
DB="$(if [ "$RUNNER" = root ]; then adb shell ls "/data/data/$PKG/databases"; else adb shell run-as "$PKG" ls databases; fi | tr -d '\r' | grep -v -- '-journal\|-wal\|-shm' | head -1)"
if [ "$RUNNER" = root ]; then
  REQ=$(printf '{"db":"/data/data/%s/databases/%s","sql":"SELECT name FROM sqlite_master","mode":"read","limit":50}' "$PKG" "$DB" | base64 | tr -d '\n')
  adb exec-out "CLASSPATH=/data/local/tmp/dbq.jar app_process / dbq.Main $REQ"
else
  REQ=$(printf '{"db":"databases/%s","sql":"SELECT name FROM sqlite_master","mode":"read","limit":50}' "$DB" | base64 | tr -d '\n')
  adb exec-out run-as "$PKG" sh -c "CLASSPATH=code_cache/dbq.jar app_process / dbq.Main $REQ"
fi
echo
echo "recent SELinux denials (empty is good):"
adb logcat -d 2>/dev/null | grep -i "avc: *denied" | grep -i "runas\|app_process\|dbq" | tail -5

step "6. MCP server tools from the terminal"
export ANDROID_PACKAGE="$PKG" ANDROID_DB_JAR="$PWD/runner/build/dbq.jar"
python3 -c "import mcp" 2>/dev/null || { echo "installing mcp python package"; pip3 install -q mcp || python3 -m pip install -q mcp || fail "pip install mcp failed"; }
python3 android_db_mcp.py call backend_status || fail "backend_status"
python3 android_db_mcp.py call list_databases || fail "list_databases"
python3 android_db_mcp.py call schema db="$DB" || fail "schema"

step "7. tenant databases in external app-specific storage"
EXT_DB="$(python3 android_db_mcp.py call list_databases | grep -o '"/storage/emulated/0/[^"]*"' | head -1 | tr -d '"')"
if [ -n "$EXT_DB" ]; then
  echo "first external db: $EXT_DB"
  python3 android_db_mcp.py call schema db="$EXT_DB" | head -60 || fail "schema on external db"
else
  echo "no external databases found (is the app logged in to a tenant?)"
fi
echo
echo "ALL STEPS PASSED  (runner=$RUNNER, db=$DB, log: $LOG)"
