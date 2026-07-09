#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Test-run the web-UI version of the console SIDE-BY-SIDE with an existing
# install, without modifying it. Reuses the existing install's venv (Kivy et al
# are already there) and its wfpiconsole.ini, and installs the two extra web
# dependencies into that venv.
#
# Usage (on the Pi, from this checkout):
#   wfpiconsole stop        # free the display + station connection first
#   bash run-web.sh
#   # then browse another device to  http://<pi-ip>:8000
#
# Override the install location if it isn't ~/wfpiconsole:
#   WFPICONSOLE_INSTALL=/path/to/wfpiconsole bash run-web.sh
# ---------------------------------------------------------------------------
set -e

INSTALL="${WFPICONSOLE_INSTALL:-$HOME/wfpiconsole}"
VENV_PY="$INSTALL/venv/bin/python3"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$VENV_PY" ]; then
    echo "Could not find the existing install's venv python at: $VENV_PY"
    echo "Set WFPICONSOLE_INSTALL to your wfpiconsole directory and retry."
    exit 1
fi

# Reuse the existing station configuration if this copy doesn't have one yet.
if [ ! -f "$HERE/wfpiconsole.ini" ] && [ -f "$INSTALL/wfpiconsole.ini" ]; then
    echo "Copying wfpiconsole.ini from $INSTALL"
    cp "$INSTALL/wfpiconsole.ini" "$HERE/"
fi

echo "Installing web dependencies into $INSTALL/venv ..."
"$VENV_PY" -m pip install --quiet fastapi "uvicorn[standard]"

echo "Starting console (web UI on http://0.0.0.0:8000) ..."
cd "$HERE"
exec "$VENV_PY" main.py
