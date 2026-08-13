#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SOURCE="$ROOT/src/sidepulse/resources/system_audio_meter.swift"
OUTPUT=${1:-"$HOME/.local/share/sidepulse/system-audio-meter"}

mkdir -p "$(dirname -- "$OUTPUT")"
xcrun swiftc -O "$SOURCE" -o "$OUTPUT"
chmod 755 "$OUTPUT"
printf '%s\n' "Built $OUTPUT"
