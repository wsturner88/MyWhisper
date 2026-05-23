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
"$PYTHON" -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install -r requirements.txt

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

cat > "$APP/Contents/MacOS/MyWhisper" <<EOF
#!/bin/bash
export MYWHISPER_HELPER="$ROOT/MyWhisper.app/Contents/Resources/mywhisper-sysaudio"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m mywhisper
EOF
chmod +x "$APP/Contents/MacOS/MyWhisper"

echo ""
echo "Done."
echo "  Store secrets:  ./run.sh setup"
echo "  Dev mode:       ./run.sh"
echo "  App:            open MyWhisper.app"
