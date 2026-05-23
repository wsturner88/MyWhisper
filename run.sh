#!/bin/bash
# Development runner. Use ./run.sh to launch the app, or ./run.sh setup for secrets.
set -euo pipefail
cd "$(dirname "$0")"
export MYWHISPER_HELPER="$(pwd)/helper/mywhisper-sysaudio"
exec ./.venv/bin/python -m mywhisper "$@"
