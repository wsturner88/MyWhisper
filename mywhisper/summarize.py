from . import config, llm

CHUNK_WORDS = 2500


def _system_prompt(preset):
    return (
        "You are a meeting-notes assistant. Write clear, accurate, concise "
        f"notes. Focus on {preset['focus']}. "
        "Never invent details that are not in the transcript."
    )


def _chunks(text, size=CHUNK_WORDS):
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def summarize_transcript(cfg, transcript_text, preset_id=None):
    """Chunked summarization, with prompts shaped by the selected preset."""
    preset_id = preset_id or config.get_meeting_preset()
    preset = config.MEETING_PRESETS.get(preset_id, config.MEETING_PRESETS["general"])
    system = _system_prompt(preset)

    chunks = _chunks(transcript_text)
    if not chunks:
        return "## Summary\n- (empty transcript)\n"

    if len(chunks) == 1:
        notes = chunks
    else:
        notes = []
        for i, chunk in enumerate(chunks, 1):
            user = (
                f"This is part {i} of {len(chunks)} of a meeting transcript "
                f"for a **{preset['label']}**. Pull out {preset['focus']} "
                f"from this part:\n\n{chunk}"
            )
            notes.append(llm.chat(cfg, system, user, max_tokens=1024))

    combined = "\n\n".join(notes)
    final = (
        f"Below are notes from a **{preset['label']}**. Produce final meeting "
        f"notes in Markdown with exactly these sections:\n\n"
        "## Summary\n## Key Decisions\n## Action Items\n## Open Questions\n\n"
        "Use bullet points. If a section has nothing, write '- None'. "
        f"Lean into {preset['focus']}.\n\n"
        f"{combined}"
    )
    return llm.chat(cfg, system, final, max_tokens=2048)


def cleanup_dictation(cfg, text):
    system = (
        "You clean up dictated text. Fix punctuation and capitalization and "
        "remove filler words (um, uh, you know). Keep the wording and meaning "
        "intact. Output only the cleaned text, with no preamble."
    )
    return llm.chat(cfg, system, text, max_tokens=1024)
