import logging
import time

from . import config, llm

log = logging.getLogger("mywhisper")

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


def _call_llm(cfg, system, user, max_tokens, label=""):
    """Call the LLM with one warm-up retry if the first response is empty."""
    reply = llm.chat(cfg, system, user, max_tokens=max_tokens)
    if reply and reply.strip():
        log.info("LLM %s: %d chars returned", label, len(reply))
        return reply
    log.warning("LLM %s: empty response, retrying after 2s warm-up", label)
    time.sleep(2)
    reply = llm.chat(cfg, system, user, max_tokens=max_tokens)
    if reply and reply.strip():
        log.info("LLM %s (retry): %d chars returned", label, len(reply))
        return reply
    raise llm.LLMError(
        "The LLM returned an empty response twice. Common causes: the "
        "model isn't actually loaded on the server, an embedding model "
        "was selected instead of a chat model, or the model can't handle "
        "this prompt. Try a different model in Settings → LLM."
    )


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
            notes.append(_call_llm(cfg, system, user, max_tokens=1024,
                                   label=f"chunk {i}/{len(chunks)}"))

    combined = "\n\n".join(notes)
    final = (
        f"Below are notes from a **{preset['label']}**. Produce final meeting "
        f"notes in Markdown with exactly these sections:\n\n"
        "## Summary\n## Key Decisions\n## Action Items\n## Open Questions\n\n"
        "Use bullet points. If a section has nothing, write '- None'. "
        f"Lean into {preset['focus']}.\n\n"
        f"{combined}"
    )
    return _call_llm(cfg, system, final, max_tokens=2048, label="final pass")


def cleanup_dictation(cfg, text):
    system = (
        "You clean up dictated text. Fix punctuation and capitalization and "
        "remove filler words (um, uh, you know). Keep the wording and meaning "
        "intact. Output only the cleaned text, with no preamble."
    )
    return llm.chat(cfg, system, text, max_tokens=1024)
