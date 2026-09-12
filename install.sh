#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
VENV_DIR="$SCRIPT_DIR/.venv"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/mouse-heatmap"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf 'Error: Python executable not found: %s\n' "$PYTHON" >&2
    exit 1
fi

printf 'Creating virtual environment in %s\n' "$VENV_DIR"
"$PYTHON" -m venv "$VENV_DIR"

printf 'Installing mouse-position-heatmap and its dependencies...\n'
"$VENV_DIR/bin/python" -m pip install -e "$SCRIPT_DIR"

mkdir -p "$BIN_DIR"
ln -sfn "$VENV_DIR/bin/mouse-heatmap" "$LAUNCHER"

printf '\nInstalled successfully. Run it without activating the virtual environment:\n'
printf '  mouse-heatmap --help\n'

case ":${PATH:-}:" in
    *":$BIN_DIR:"*) ;;
    *)
        printf '\nNote: %s is not currently on PATH. Add this line to your shell config:\n' "$BIN_DIR"
        printf '  export PATH="%s:$PATH"\n' "$BIN_DIR"
        printf 'Until then, run: %s --help\n' "$LAUNCHER"
        ;;
esac
