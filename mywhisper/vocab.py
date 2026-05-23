"""Custom vocabulary — work-specific terms fed to Whisper as a hint so it
transcribes names, initials, and abbreviations correctly."""

from . import config


def vocab_path():
    return config.app_dir() / "vocabulary.txt"


_HEADER = (
    "# MyWhisper Vocabulary\n"
    "#\n"
    "# Add one word or name per line - company names, client names,\n"
    "# initials, abbreviations, anything that gets mis-heard.\n"
    "# Press Return for a new line. Edit or delete any line freely.\n"
    "# Save with Command-S when done; changes apply to your next dictation.\n"
    "# Lines starting with # are ignored.\n\n"
)


def ensure_file():
    path = vocab_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_HEADER)
    return path


def load_terms():
    path = vocab_path()
    try:
        if path.exists():
            return [
                line.strip()
                for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        pass
    return []


def prompt():
    """Return a Whisper initial_prompt biasing toward the custom terms."""
    terms = load_terms()
    if not terms:
        return None
    # Whisper's prompt budget is small; cap it (most-recent terms win).
    text = ", ".join(terms)
    if len(text) > 800:
        text = ", ".join(terms[-120:])[:800]
    return text
