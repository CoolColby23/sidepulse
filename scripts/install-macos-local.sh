#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILT_APP="$ROOT_DIR/build/macos-pkg/pyinstaller/SidePulse.app"
INSTALL_APP=${SIDEPULSE_APP_PATH:-/Applications/SidePulse.app}
APP_BINARY="$INSTALL_APP/Contents/MacOS/SidePulse"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/io.sidepulse.agentstatus.plist"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This installer only supports macOS." >&2
    exit 2
fi

# Prefer a real (even free "Apple Development") signing identity over an
# ad-hoc one: ad-hoc signatures key the TCC (Screen Recording, etc.) grant to
# the binary's exact content hash, so every local rebuild invalidates any
# permission already granted in System Settings. A Team ID-anchored identity
# keeps that grant valid across rebuilds.
if [ -z "${APP_SIGN_IDENTITY:-}" ]; then
    # Use the SHA-1 hash rather than the display name: multiple certs can
    # share the same "Apple Development: ..." label, which codesign refuses
    # to accept as ambiguous.
    DETECTED_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
        | grep "Apple Development:" | head -1 | awk '{print $2}')
    if [ -n "$DETECTED_IDENTITY" ]; then
        APP_SIGN_IDENTITY="$DETECTED_IDENTITY"
        echo "Signing with detected identity: $DETECTED_IDENTITY"
    fi
fi

ALLOW_UNSIGNED=1 APP_ONLY=1 APP_SIGN_IDENTITY="${APP_SIGN_IDENTITY:-}" \
    "$ROOT_DIR/packaging/build_macos_pkg.sh"

# Stop the old Python-backed process before replacing the application bundle.
launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT" >/dev/null 2>&1 || true
pkill -TERM -x SidePulse >/dev/null 2>&1 || true
sleep 1
/usr/bin/ditto "$BUILT_APP" "$INSTALL_APP"

# Running setup from the frozen executable makes hooks and the LaunchAgent
# retain SidePulse's application identity instead of the build-time Python.
"$APP_BINARY" setup "$@"

echo "Installed local SidePulse app: $INSTALL_APP"
