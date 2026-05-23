import json
import logging
from datetime import datetime
from pathlib import Path

from . import config

_MAX_ENTRIES = 20
_LOG_FILE = config.APP_DIR / "dictation_history.json"
log = logging.getLogger("mywhisper")


def _load():
    try:
        if _LOG_FILE.exists():
            return json.loads(_LOG_FILE.read_text())
    except Exception:
        log.exception("dictation_log: failed to load")
    return []


def _save(entries):
    try:
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.write_text(json.dumps(entries, indent=2))
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
