#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python3}
INSTALL_ROOT=${SIDEPULSE_INSTALL_ROOT:-"$HOME/.local/share/sidepulse"}
BIN_DIR=${SIDEPULSE_BIN_DIR:-"$HOME/.local/bin"}
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VENV_DIR="$INSTALL_ROOT/venv"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$SOURCE_DIR"

mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/sidepulse" "$BIN_DIR/sidepulse"
ln -sf "$VENV_DIR/bin/agent-monitor" "$BIN_DIR/agent-monitor"

printf '%s\n' "SidePulse installed in $VENV_DIR"
printf '%s\n' "CLI linked at $BIN_DIR/sidepulse"
printf '%s\n' "Run: $BIN_DIR/sidepulse setup"
