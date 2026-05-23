import getpass
import sys


def _setup():
    from . import config

    print("MyWhisper setup - secrets are stored in your macOS Keychain.")
    print("Leave a line blank to skip it.\n")

    items = [
        ("hf_token", "Hugging Face token (for speaker diarization)"),
        ("anthropic_api_key", "Anthropic API key (only if using Claude)"),
        ("openrouter_api_key", "OpenRouter API key (only if using OpenRouter)"),
    ]
    for key, label in items:
        existing = " [already set]" if config.get_secret(key) else ""
        value = getpass.getpass(f"{label}{existing}: ").strip()
        if value:
            config.set_secret(key, value)
            print("  saved.")

    print(f"\nDone. Config file: {config.CONFIG_PATH}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        _setup()
        return
    from .app import MyWhisperApp
    MyWhisperApp().run()


if __name__ == "__main__":
    main()
