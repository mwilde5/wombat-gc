#!/bin/bash
# macOS / Linux launcher: double-click (macOS) or run ./start_wombatgc.command [file]
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "WombatGC needs Python 3.10 or newer (3.12 recommended)."
    echo "The python3 built into macOS is too old: install Python from https://www.python.org/downloads/"
    echo "then double-click this file again."
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
  fi
  echo "First run: setting up a private Python environment with $PY (needs internet, a minute or two)..."
  "$PY" -m venv .venv || { echo "Could not create the environment."; exit 1; }
  .venv/bin/python -m pip install --upgrade pip
fi
if ! cmp -s requirements.txt .venv/installed-requirements.txt; then
  echo "Installing / updating packages..."
  .venv/bin/python -m pip install -r requirements.txt || { read -n 1 -s -r -p "Package installation failed - press any key"; exit 1; }
  cp requirements.txt .venv/installed-requirements.txt
fi
exec .venv/bin/python wombat.py "$@"
