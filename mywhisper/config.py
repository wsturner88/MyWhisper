import json
import os
import shutil
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import keyring

# A small pointer file at a fixed, OS-standard location tells us WHERE the
# user's data folder lives. This lets the user move ~/MyWhisper to e.g.
# ~/Documents/MyWhisper without losing config.
POINTER_DIR = Path.home() / "Library" / "Application Support" / "MyWhisper"
POINTER_PATH = POINTER_DIR / "data_location.txt"
LEGACY_APP_DIR = Path.home() / "MyWhisper"
DEFAULT_APP_DIR = Path.home() / "Documents" / "MyWhisper"

KEYCHAIN_SERVICE = "MyWhisper"


def _initial_location():
    """Choose where the data folder lives the very first time the app runs.

    If the legacy ~/MyWhisper already has data, keep using it so we don't
    surprise existing users. Otherwise default to ~/Documents/MyWhisper.
    """
    if LEGACY_APP_DIR.exists() and any(LEGACY_APP_DIR.iterdir()):
        return LEGACY_APP_DIR
    return DEFAULT_APP_DIR


def _load_pointer():
    try:
        if POINTER_PATH.exists():
            txt = POINTER_PATH.read_text().strip()
            if txt:
                return Path(os.path.expanduser(txt))
    except Exception:
        pass
    return None


def _write_pointer(path):
    try:
        POINTER_DIR.mkdir(parents=True, exist_ok=True)
        POINTER_PATH.write_text(str(Path(path).expanduser()))
    except Exception:
        pass


def app_dir():
    """Return the user's MyWhisper data folder, creating it if needed."""
    loc = _load_pointer()
    if loc is None:
        loc = _initial_location()
        _write_pointer(loc)
    loc.mkdir(parents=True, exist_ok=True)
    return loc


def set_app_dir(new_path, move_existing=True):
    """Move the data folder to a new location.

    If move_existing is True and the old folder has files, copy them over
    to the new location. Returns the resolved new path.
    """
    new_path = Path(os.path.expanduser(str(new_path))).resolve()
    new_path.mkdir(parents=True, exist_ok=True)
    old_path = app_dir()
    if move_existing and old_path != new_path:
        for item in old_path.iterdir():
            target = new_path / item.name
            if target.exists():
                continue
            try:
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            except Exception:
                pass
    _write_pointer(new_path)
    return new_path


# -- Paths that live inside app_dir() ---------------------------------------
# Use the functions, not module-level constants, so a runtime change to the
# data location is picked up immediately.

def config_path():
    return app_dir() / "config.toml"


def selected_mic_path():
    return app_dir() / "selected_mic"


def viz_path():
    return app_dir() / "visualization"


def llm_provider_path():
    return app_dir() / "llm_provider"


def llm_model_path():
    return app_dir() / "llm_model"


def meeting_preset_path():
    return app_dir() / "meeting_preset"


def log_path():
    return app_dir() / "mywhisper.log"


# Back-compat shims — older code reads config.APP_DIR / config.CONFIG_PATH.
class _LazyPath:
    def __init__(self, getter):
        self._getter = getter

    def __truediv__(self, other):
        return self._getter() / other

    def __str__(self):
        return str(self._getter())

    def __fspath__(self):
        return os.fspath(self._getter())

    def __getattr__(self, name):
        return getattr(self._getter(), name)


APP_DIR = _LazyPath(app_dir)
CONFIG_PATH = _LazyPath(config_path)
SELECTED_MIC_PATH = _LazyPath(selected_mic_path)
VIZ_PATH = _LazyPath(viz_path)
LLM_PROVIDER_PATH = _LazyPath(llm_provider_path)
LLM_MODEL_PATH = _LazyPath(llm_model_path)


DEFAULT_CONFIG = '''# MyWhisper configuration.

[hotkeys]
# Hold this key to dictate (push-to-talk). Options: right_option,
# right_command, right_control, right_shift, left_option, left_command
push_to_talk = "right_option"

[whisper]
# Any MLX Whisper model on Hugging Face.
# Balanced: large-v3-turbo. Best accuracy: large-v3. Fastest: small.
model = "mlx-community/whisper-large-v3-turbo"

[diarization]
enabled = true

[llm]
# Provider, API key, and model are set via the dashboard Settings tab.
provider = "openrouter"
openrouter_model = "anthropic/claude-sonnet-4-6"
anthropic_model = "claude-sonnet-4-6"

[dictation]
cleanup = false

[sounds]
enabled = true
start = "Tink"
stop = "Pop"
'''


LLM_PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "key_name": "openrouter_api_key",
        "default_model": "anthropic/claude-sonnet-4-6",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_name": "anthropic_api_key",
        "default_model": "claude-sonnet-4-6",
    },
}


BUILTIN_PRESETS = {
    "general": {
        "label": "General Meeting",
        "description": "Standard meeting notes — summary, decisions, action items.",
        "focus": (
            "the key discussion points, decisions reached, action items "
            "with owners, and open questions"
        ),
    },
    "sales_call": {
        "label": "Sales Call",
        "description": "Customer needs, objections, next steps.",
        "focus": (
            "the customer's pain points and goals, objections or concerns "
            "raised, pricing or budget discussion, competitive mentions, "
            "and specific next steps with timing and owners"
        ),
    },
    "standup": {
        "label": "Internal Standup",
        "description": "What's done, what's next, blockers.",
        "focus": (
            "what each person completed since the last standup, what "
            "they're working on next, and any blockers needing help"
        ),
    },
}

DEFAULT_CUSTOM_PRESET = {
    "label": "My Custom Meeting",
    "description": "Your own prompt — define what the AI should focus on.",
    "focus": (
        "the most important points, any decisions or commitments made, "
        "and any follow-up items"
    ),
}


def custom_preset_path():
    return app_dir() / "custom_preset.json"


def get_custom_preset():
    """Return the user-defined preset dict (label, description, focus)."""
    try:
        path = custom_preset_path()
        if path.exists():
            data = json.loads(path.read_text())
            return {
                "label": data.get("label") or DEFAULT_CUSTOM_PRESET["label"],
                "description": (data.get("description")
                                or DEFAULT_CUSTOM_PRESET["description"]),
                "focus": data.get("focus") or DEFAULT_CUSTOM_PRESET["focus"],
            }
    except Exception:
        pass
    return dict(DEFAULT_CUSTOM_PRESET)


def set_custom_preset(label, focus):
    label = (label or "").strip() or DEFAULT_CUSTOM_PRESET["label"]
    focus = (focus or "").strip() or DEFAULT_CUSTOM_PRESET["focus"]
    try:
        custom_preset_path().write_text(json.dumps({
            "label": label,
            "description": "Your custom prompt.",
            "focus": focus,
        }, indent=2))
    except Exception:
        pass


def meeting_presets():
    """Built-in presets plus the user's custom preset, in order."""
    presets = dict(BUILTIN_PRESETS)
    presets["custom"] = get_custom_preset()
    return presets


# Module-level dict-like accessor so existing references like
# config.MEETING_PRESETS keep working but stay live.
class _PresetsView:
    def __getitem__(self, key):
        return meeting_presets()[key]

    def get(self, key, default=None):
        return meeting_presets().get(key, default)

    def items(self):
        return meeting_presets().items()

    def keys(self):
        return meeting_presets().keys()

    def values(self):
        return meeting_presets().values()

    def __contains__(self, key):
        return key in meeting_presets()

    def __iter__(self):
        return iter(meeting_presets())


MEETING_PRESETS = _PresetsView()


def _ensure_config_file():
    cp = config_path()
    if not cp.exists():
        cp.write_text(DEFAULT_CONFIG)


def load():
    _ensure_config_file()
    with open(config_path(), "rb") as f:
        return tomllib.load(f)


def output_dir(cfg=None):
    """Where meeting summaries are saved. Lives inside app_dir()."""
    return app_dir()


def get_secret(name):
    return keyring.get_password(KEYCHAIN_SERVICE, name)


def set_secret(name, value):
    keyring.set_password(KEYCHAIN_SERVICE, name, value)


def get_selected_mic():
    try:
        p = selected_mic_path()
        if p.exists():
            return p.read_text().strip() or None
    except OSError:
        pass
    return None


def set_selected_mic(name):
    try:
        selected_mic_path().write_text(name or "")
    except OSError:
        pass


def get_visualization():
    try:
        p = viz_path()
        if p.exists():
            return p.read_text().strip() or "waveform"
    except OSError:
        pass
    return "waveform"


def set_visualization(kind):
    try:
        viz_path().write_text(kind or "")
    except OSError:
        pass


def get_llm_provider():
    try:
        p = llm_provider_path()
        if p.exists():
            val = p.read_text().strip()
            if val in LLM_PROVIDERS:
                return val
    except OSError:
        pass
    return "openrouter"


def set_llm_provider(provider):
    try:
        llm_provider_path().write_text(provider or "openrouter")
    except OSError:
        pass


def get_llm_model(provider):
    try:
        p = llm_model_path()
        if p.exists():
            parts = p.read_text().strip().split(":", 1)
            if len(parts) == 2 and parts[0] == provider:
                return parts[1]
    except OSError:
        pass
    return LLM_PROVIDERS.get(provider, {}).get("default_model", "")


def set_llm_model(provider, model):
    try:
        llm_model_path().write_text(f"{provider}:{model}")
    except OSError:
        pass


def get_meeting_preset():
    try:
        p = meeting_preset_path()
        if p.exists():
            val = p.read_text().strip()
            if val in MEETING_PRESETS:
                return val
    except OSError:
        pass
    return "general"


def set_meeting_preset(preset_id):
    if preset_id not in MEETING_PRESETS:
        preset_id = "general"
    try:
        meeting_preset_path().write_text(preset_id)
    except OSError:
        pass
