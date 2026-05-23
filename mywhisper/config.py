import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import keyring

APP_DIR = Path.home() / "MyWhisper"
CONFIG_PATH = APP_DIR / "config.toml"
SELECTED_MIC_PATH = APP_DIR / "selected_mic"
VIZ_PATH = APP_DIR / "visualization"
LLM_PROVIDER_PATH = APP_DIR / "llm_provider"
LLM_MODEL_PATH = APP_DIR / "llm_model"
KEYCHAIN_SERVICE = "MyWhisper"

DEFAULT_CONFIG = '''# MyWhisper configuration.

[hotkeys]
# Hold this key to dictate (push-to-talk). Release it and the text is typed
# wherever your cursor is. Options: right_option, right_command,
# right_control, right_shift, left_option, left_command
push_to_talk = "right_option"

[whisper]
# Any MLX Whisper model on Hugging Face.
# Balanced: large-v3-turbo. Best accuracy: large-v3. Fastest: small.
model = "mlx-community/whisper-large-v3-turbo"

[diarization]
# Speaker separation. Needs a Hugging Face token (see README).
enabled = true

[llm]
# provider: "openrouter" | "anthropic"
provider = "openrouter"

openrouter_model = "anthropic/claude-sonnet-4-6"

anthropic_model = "claude-sonnet-4-6"

[dictation]
# Optional LLM cleanup (removes "um"/"uh"). Sends text to your LLM, adding a
# round-trip — keep false for instant, fully on-device dictation.
cleanup = false

[output]
# Keep this OUTSIDE any cloud-synced folder.
dir = "~/MyWhisper"

[sounds]
# Audio cue when push-to-talk recording starts and stops. enabled = false
# to mute. start/stop are macOS sound names from /System/Library/Sounds
# (e.g. Tink, Pop, Bottle, Glass, Ping, Submarine).
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


def _ensure():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)


def load():
    _ensure()
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def output_dir(cfg):
    path = Path(os.path.expanduser(cfg["output"]["dir"]))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_secret(name):
    return keyring.get_password(KEYCHAIN_SERVICE, name)


def set_secret(name, value):
    keyring.set_password(KEYCHAIN_SERVICE, name, value)


def get_selected_mic():
    try:
        if SELECTED_MIC_PATH.exists():
            return SELECTED_MIC_PATH.read_text().strip() or None
    except OSError:
        pass
    return None


def set_selected_mic(name):
    try:
        SELECTED_MIC_PATH.write_text(name or "")
    except OSError:
        pass


def get_visualization():
    try:
        if VIZ_PATH.exists():
            return VIZ_PATH.read_text().strip() or "waveform"
    except OSError:
        pass
    return "waveform"


def set_visualization(kind):
    try:
        VIZ_PATH.write_text(kind or "")
    except OSError:
        pass


def get_llm_provider():
    try:
        if LLM_PROVIDER_PATH.exists():
            val = LLM_PROVIDER_PATH.read_text().strip()
            if val in LLM_PROVIDERS:
                return val
    except OSError:
        pass
    return "openrouter"


def set_llm_provider(provider):
    try:
        LLM_PROVIDER_PATH.write_text(provider or "openrouter")
    except OSError:
        pass


def get_llm_model(provider):
    try:
        if LLM_MODEL_PATH.exists():
            parts = LLM_MODEL_PATH.read_text().strip().split(":", 1)
            if len(parts) == 2 and parts[0] == provider:
                return parts[1]
    except OSError:
        pass
    return LLM_PROVIDERS.get(provider, {}).get("default_model", "")


def set_llm_model(provider, model):
    try:
        LLM_MODEL_PATH.write_text(f"{provider}:{model}")
    except OSError:
        pass
