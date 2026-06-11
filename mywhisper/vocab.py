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


def _term_only(line):
    """Strip annotations: 'Marty Cohen - StewartEFI - Sales Manager' ->
    'Marty Cohen'. The annotations (roles, 'heard as' mishearings) must
    never reach Whisper — they bias it toward the wrong words."""
    for sep in (" - ", " — ", " – "):
        if sep in line:
            line = line.split(sep)[0]
    return line.strip()


def prompt():
    """Return a Whisper initial_prompt biasing toward the custom terms.

    A bare comma-joined name list makes Whisper's decoder occasionally
    *continue the list* instead of transcribing the audio (looping
    'Michael Cohen, Michael Cohen, …'), so the terms are wrapped in a
    sentence and kept short. transcribe_array() retries without the
    prompt if the result still comes back as hallucinated junk.
    """
    terms = []
    for line in load_terms():
        if line.endswith(":"):  # section header, not a term
            continue
        term = _term_only(line)
        if term and term not in terms:
            terms.append(term)
    if not terms:
        return None
    # Whisper's prompt budget is small (224 tokens, oldest cut first);
    # stay well under it so every term keeps its influence.
    kept = []
    used = 0
    for term in terms:
        used += len(term) + 2
        if used > 600:
            break
        kept.append(term)
    return "Notes may mention: " + ", ".join(kept) + "."
