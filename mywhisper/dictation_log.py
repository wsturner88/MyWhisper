import json
import logging
from datetime import datetime
from pathlib import Path

from . import config

_MAX_ENTRIES = 20
log = logging.getLogger("mywhisper")


def _log_file():
    return config.app_dir() / "dictation_history.json"


def _load():
    try:
        path = _log_file()
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        log.exception("dictation_log: failed to load")
    return []


def _save(entries):
    try:
        config.app_dir().mkdir(parents=True, exist_ok=True)
        _log_file().write_text(json.dumps(entries, indent=2))
    except Exception:
        log.exception("dictation_log: failed to save")


def add(text):
    entries = _load()
    entries.insert(0, {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
    })
    _save(entries[:_MAX_ENTRIES])


def recent():
    return _load()[:_MAX_ENTRIES]
