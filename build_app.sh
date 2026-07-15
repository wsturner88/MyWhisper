#!/bin/bash
# Builds the Python environment, compiles the system-audio helper, and
# assembles MyWhisper.app. Re-run this after changing requirements or the helper.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo "==> [1/3] Python environment"
PYTHON=""
for candidate in python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  PYTHON="python3"
  echo "    WARNING: Python 3.10+ not found, falling back to $($PYTHON --version 2>&1)."
  echo "    mlx-whisper and pyannote work best on Python 3.11/3.12."
  echo "    Install a newer Python with:  brew install python@3.12"
fi
echo "    using $PYTHON ($($PYTHON --version 2>&1))"
# The venv lives OUTSIDE the project folder: this folder syncs through
# OneDrive, and a synced venv corrupts (dataless placeholders, conflict
# copies) once a second Mac joins the sync.
VENV="${MYWHISPER_VENV:-$HOME/.mywhisper-venv}"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install -r requirements.txt

echo "==> [2/3] System-audio helper (Swift / ScreenCaptureKit)"
if ! command -v swiftc >/dev/null 2>&1; then
  echo "ERROR: swiftc not found. Install Xcode Command Line Tools:" >&2
  echo "       xcode-select --install" >&2
  exit 1
fi
swiftc -O -target arm64-apple-macos13.0 \
  -o helper/mywhisper-sysaudio helper/SystemAudioRecorder.swift

echo "==> [3/3] MyWhisper.app bundle"
APP="MyWhisper.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
cp helper/mywhisper-sysaudio "$APP/Contents/Resources/mywhisper-sysaudio"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/MacOS/MyWhisper" <<EOF
#!/bin/bash
# Start (or restart) MyWhisper. When the start-at-login service is
# installed, kick it — that guarantees a single instance. Otherwise run
# the app directly.
if launchctl print "gui/\$(id -u)/local.mywhisper" >/dev/null 2>&1; then
  exec launchctl kickstart -k "gui/\$(id -u)/local.mywhisper"
fi
export MYWHISPER_HELPER="$ROOT/MyWhisper.app/Contents/Resources/mywhisper-sysaudio"
cd "$ROOT"
exec "\${MYWHISPER_VENV:-\$HOME/.mywhisper-venv}/bin/python" -m mywhisper
EOF
chmod +x "$APP/Contents/MacOS/MyWhisper"

echo ""
echo "Done."
echo "  Store secrets:  ./run.sh setup"
echo "  Dev mode:       ./run.sh"
echo "  App:            open MyWhisper.app"
