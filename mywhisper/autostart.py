"""Optional 'start at login' via a per-user LaunchAgent.

The agent launches the app directly as a Python process. Launching via a
.app bundle prevents the menu bar item from rendering on macOS, so the
app must always start as a naked process.
"""

import plistlib
import subprocess
from pathlib import Path

LABEL = "local.mywhisper"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _project_root():
    return Path(__file__).resolve().parent.parent


def is_enabled():
    return PLIST_PATH.exists()


def enable():
    root = _project_root()
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            str(root / ".venv" / "bin" / "python"), "-m", "mywhisper"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "MYWHISPER_HELPER": str(root / "helper" / "mywhisper-sysaudio")},
        "RunAtLoad": True,
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)


def disable():
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   check=False, capture_output=True)
    try:
        PLIST_PATH.unlink()
    except OSError:
        pass
