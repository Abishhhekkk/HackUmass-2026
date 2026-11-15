#!/usr/bin/env bash
# Vantage drone side (Jetson): one command. Options: ./vantage-edge.sh --help
cd "$(dirname "$(readlink -f "$0")")" || exit 1
if [ -f "$HOME/rescue-venv/bin/activate" ]; then
  source "$HOME/rescue-venv/bin/activate"
elif [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi
exec python3 scripts/launch_edge.py "$@"
