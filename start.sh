#!/usr/bin/env bash
# Claude chat vault: first run scans this computer and writes config.json, then opens the dashboard.
# Keep this terminal open while you use the dashboard.  Usage: bash start.sh
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it (https://www.python.org/downloads/ or your package manager)."
  exit 1
fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8 or newer is needed."; exit 1; }
exec "$PY" vault.py start "$@"
