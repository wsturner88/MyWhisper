"""Custom vocabulary — work-specific terms fed to Whisper as a hint so it
transcribes names, initials, and abbreviations correctly."""

from . import config

VOCAB_PATH = config.APP_DIR / "vocabulary.txt"

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
    VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not VOCAB_PATH.exists():
        VOCAB_PATH.write_text(_HEADER)
    return VOCAB_PATH


def load_terms():
    try:
        if VOCAB_PATH.exists():
            return [
                line.strip()
                for line in VOCAB_PATH.read_text().splitlines()
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
