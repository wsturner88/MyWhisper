"""Short audio cues for push-to-talk start/stop, using macOS system sounds."""

import subprocess
from pathlib import Path

_SOUND_DIR = Path("/System/Library/Sounds")


def _play(name):
    path = _SOUND_DIR / f"{name}.aiff"
    if not path.exists():
        return
    try:
        subprocess.Popen(
            ["afplay", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_start(cfg):
    section = cfg.get("sounds", {})
    if section.get("enabled", True):
        _play(section.get("start", "Tink"))


def play_stop(cfg):
    section = cfg.get("sounds", {})
    if section.get("enabled", True):
        _play(section.get("stop", "Pop"))


def play_done(cfg):
    """Played when a meeting summary is finished and saved.

    Default is 'Glass' — a friendly chime that's clearly distinct from the
    start/stop ticks so it reads as 'all done!'.
    """
    section = cfg.get("sounds", {})
    if section.get("enabled", True):
        _play(section.get("done", "Glass"))


def play_failed(cfg):
    """Played when a meeting summary fails — a softer cue so it's not
    alarming but tells the user to look at the result."""
    section = cfg.get("sounds", {})
    if section.get("enabled", True):
        _play(section.get("failed", "Funk"))
