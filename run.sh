#!/bin/bash
# Development runner. Use ./run.sh to launch the app, or ./run.sh setup for secrets.
#
# The Python environment lives OUTSIDE the project folder on purpose: this
# folder syncs through OneDrive, and a synced venv gets corrupted (cloud-only
# placeholder files, "python 2" conflict copies) as soon as a second Mac
# joins the sync. Rebuild it any time with ./build_app.sh.
set -euo pipefail
cd "$(dirname "$0")"
VENV="${MYWHISPER_VENV:-$HOME/.mywhisper-venv}"
export MYWHISPER_HELPER="$(pwd)/helper/mywhisper-sysaudio"
exec "$VENV/bin/python" -m mywhisper "$@"
