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

ALLOW_UNSIGNED=1 APP_ONLY=1 "$ROOT_DIR/packaging/build_macos_pkg.sh"

# Stop the old Python-backed process before replacing the application bundle.
launchctl bootout "gui/$(id -u)" "$LAUNCH_AGENT" >/dev/null 2>&1 || true
/usr/bin/ditto "$BUILT_APP" "$INSTALL_APP"

# Running setup from the frozen executable makes hooks and the LaunchAgent
# retain SidePulse's application identity instead of the build-time Python.
"$APP_BINARY" setup "$@"

echo "Installed local SidePulse app: $INSTALL_APP"
