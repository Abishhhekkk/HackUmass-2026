#!/usr/bin/env bash
# Vantage, drone side (Jetson). Double-click (after ./Vantage-Jetson.sh --install) or run ./Vantage-Jetson.sh
#   --setup    re-enter the cloud address and ingest key
#   --install  add a "Vantage (Jetson)" icon to the desktop and app menu
cd "$(dirname "$(readlink -f "$0")")" || exit 1
VENV="${VANTAGE_VENV:-$HOME/rescue-venv}"
if [ ! -f "$VENV/bin/activate" ]; then
  echo "Python environment $VENV not found (set VANTAGE_VENV to its folder)."
  read -rp "Press Enter to close" _; exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
if [ "${VANTAGE_NO_PULL:-0}" != 1 ] && git diff --quiet 2>/dev/null; then
  if timeout 20 git pull --ff-only -q 2>/dev/null; then echo "Code up to date."; else echo "No update (offline or local changes); using the code on this Jetson."; fi
fi
python scripts/vantage_jetson.py "$@"
status=$?
[ "$1" = "--install" ] || read -rp "Press Enter to close this window" _
exit $status
