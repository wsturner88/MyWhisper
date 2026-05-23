from . import llm

CHUNK_WORDS = 2500

_SYSTEM = (
    "You are a meeting-notes assistant. Write clear, accurate, concise notes. "
    "Never invent details that are not in the transcript."
)


def _chunks(text, size=CHUNK_WORDS):
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def summarize_transcript(cfg, transcript_text):
    """Chunked summarization so a small local model never overflows context."""
    chunks = _chunks(transcript_text)
    if not chunks:
        return "## Summary\n- (empty transcript)\n"

    if len(chunks) == 1:
        notes = chunks
    else:
        notes = []
        for i, chunk in enumerate(chunks, 1):
            user = (
                f"This is part {i} of {len(chunks)} of a meeting transcript. "
                f"List the key points, decisions, and action items in this "
                f"part:\n\n{chunk}"
            )
            notes.append(llm.chat(cfg, _SYSTEM, user, max_tokens=1024))

    combined = "\n\n".join(notes)
    final = (
        "Below are notes from a meeting. Produce final meeting notes in "
        "Markdown with exactly these sections:\n\n"
        "## Summary\n## Key Decisions\n## Action Items\n## Open Questions\n\n"
        "Use bullet points. If a section has nothing, write '- None'.\n\n"
        f"{combined}"
    )
    return llm.chat(cfg, _SYSTEM, final, max_tokens=2048)


def cleanup_dictation(cfg, text):
    system = (
        "You clean up dictated text. Fix punctuation and capitalization and "
        "remove filler words (um, uh, you know). Keep the wording and meaning "
        "intact. Output only the cleaned text, with no preamble."
    )
    return llm.chat(cfg, system, text, max_tokens=1024)
