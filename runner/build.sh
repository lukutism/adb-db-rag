#!/usr/bin/env bash
# Builds build/dbq.jar (a dex jar) from src/dbq/Main.java using the Android SDK on this machine.
# Needs: a JDK (javac), and $ANDROID_HOME (or ~/Library/Android/sdk) with one platform + build-tools.
set -euo pipefail
cd "$(dirname "$0")"

ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
PLATFORM_JAR="$(ls -d "$ANDROID_HOME"/platforms/android-*/android.jar | sort -V | tail -1)"
D8="$(ls -d "$ANDROID_HOME"/build-tools/*/d8 | sort -V | tail -1)"
MIN_API="${MIN_API:-26}"

echo "platform : $PLATFORM_JAR"
echo "d8       : $D8"

rm -rf build && mkdir -p build/classes
javac --release 8 -Xlint:-options -cp "$PLATFORM_JAR" -d build/classes src/dbq/Main.java
"$D8" --release --min-api "$MIN_API" --lib "$PLATFORM_JAR" --output build/dbq.jar build/classes/dbq/*.class

echo "built    : $(pwd)/build/dbq.jar ($(wc -c < build/dbq.jar) bytes)"
unzip -l build/dbq.jar | grep -q classes.dex && echo "ok: classes.dex present"
