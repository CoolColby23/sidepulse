#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -1)"
ARCH="$(uname -m)"
BUILD_DIR="$ROOT_DIR/build/macos-pkg"
DIST_DIR="$ROOT_DIR/dist"
VENV_DIR="$BUILD_DIR/venv"
APP_PATH="$BUILD_DIR/pyinstaller/SidePulse.app"
COMPONENT_PKG="$BUILD_DIR/SidePulse-component.pkg"
OUTPUT_PKG="$DIST_DIR/SidePulse-${VERSION}-${ARCH}.pkg"
APP_ID="${APP_ID:-io.sidepulse.cli}"
SYSTEM_AUDIO_HELPER="$BUILD_DIR/system-audio-meter"

APP_SIGN_IDENTITY="${APP_SIGN_IDENTITY:-}"
INSTALLER_SIGN_IDENTITY="${INSTALLER_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
ALLOW_UNSIGNED="${ALLOW_UNSIGNED:-0}"
APP_ONLY="${APP_ONLY:-0}"

if { [ -z "$APP_SIGN_IDENTITY" ] || [ -z "$INSTALLER_SIGN_IDENTITY" ]; } && [ "$ALLOW_UNSIGNED" != "1" ]; then
    echo "Set APP_SIGN_IDENTITY to a Developer ID Application identity and" >&2
    echo "INSTALLER_SIGN_IDENTITY to a Developer ID Installer identity." >&2
    exit 2
fi

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install 'pyinstaller>=6.10' "$ROOT_DIR"

xcrun swiftc -parse-as-library -O \
    "$ROOT_DIR/src/sidepulse/resources/system_audio_meter.swift" \
    -o "$SYSTEM_AUDIO_HELPER"

"$VENV_DIR/bin/pyinstaller" \
    --noconfirm --clean --windowed \
    --name SidePulse \
    --osx-bundle-identifier "$APP_ID" \
    --distpath "$BUILD_DIR/pyinstaller" \
    --workpath "$BUILD_DIR/work" \
    --specpath "$BUILD_DIR" \
    --collect-submodules Cocoa \
    --collect-data sidepulse.resources \
    --add-binary "$SYSTEM_AUDIO_HELPER:." \
    "$ROOT_DIR/packaging/sidepulse_entry.py"

/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :NSScreenCaptureUsageDescription string SidePulse uses system audio levels to animate LEDs when the optional Idle Music Visualizer is enabled." "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :NSScreenCaptureUsageDescription SidePulse uses system audio levels to animate LEDs when the optional Idle Music Visualizer is enabled." "$APP_PATH/Contents/Info.plist"

if [ -n "$APP_SIGN_IDENTITY" ]; then
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ROOT_DIR/packaging/entitlements.plist" \
        --sign "$APP_SIGN_IDENTITY" "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
    codesign --force --deep --sign - "$APP_PATH"
fi

if [ "$APP_ONLY" = "1" ]; then
    echo "Built app: $APP_PATH"
    exit 0
fi

chmod 755 "$ROOT_DIR/packaging/scripts/postinstall"
export COPYFILE_DISABLE=1
pkgbuild \
    --component "$APP_PATH" \
    --install-location /Applications \
    --identifier "$APP_ID" \
    --version "$VERSION" \
    --scripts "$ROOT_DIR/packaging/scripts" \
    "$COMPONENT_PKG"
if [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
    productbuild --package "$COMPONENT_PKG" \
        --sign "$INSTALLER_SIGN_IDENTITY" \
        --timestamp "$OUTPUT_PKG"
    pkgutil --check-signature "$OUTPUT_PKG"
else
    productbuild --package "$COMPONENT_PKG" "$OUTPUT_PKG"
fi

if [ -n "$NOTARY_PROFILE" ] && [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
    xcrun notarytool submit "$OUTPUT_PKG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$OUTPUT_PKG"
    xcrun stapler validate "$OUTPUT_PKG"
else
    echo "Built package: $OUTPUT_PKG"
    if [ -n "$INSTALLER_SIGN_IDENTITY" ]; then
        echo "Set NOTARY_PROFILE to submit and staple it automatically."
    else
        echo "Local verification build only: this package is not Developer ID signed or notarized."
    fi
fi
