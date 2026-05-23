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
    """Chunked summarization, with prompts shaped by the selected preset.

    Returns a (title, summary_md) tuple. Title is a short LLM-generated
    label (e.g. 'Sales call with Acme — pricing review'). If the title
    pass fails, returns an empty title and the caller falls back to a
    timestamp-only filename.
    """
    preset_id = preset_id or config.get_meeting_preset()
    preset = config.MEETING_PRESETS.get(preset_id, config.MEETING_PRESETS["general"])
    system = _system_prompt(preset)

    chunks = _chunks(transcript_text)
    if not chunks:
        return "", "## Summary\n- (empty transcript)\n"

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
    summary = _call_llm(cfg, system, final, max_tokens=2048, label="final pass")
    title = _generate_title(cfg, summary, transcript_text)
    return title, summary


def _generate_title(cfg, summary, transcript):
    """Ask the LLM for a short title summarizing the meeting. Returns ''
    on failure — caller falls back to a timestamp-only filename."""
    # Use the summary if we have one; otherwise fall back to a chunk of
    # the raw transcript.
    source = (summary or transcript or "").strip()
    if not source:
        return ""
    source = source[:1500]  # keep the prompt short
    system = (
        "You produce short, descriptive titles for meeting notes. "
        "Reply with ONLY the title — no quotes, no explanation, "
        "no trailing punctuation."
    )
    user = (
        "In 3 to 7 words, give a title that captures what this meeting was "
        "about. Use plain English. No quotes.\n\n"
        f"{source}"
    )
    try:
        title = llm.chat(cfg, system, user, max_tokens=40)
    except Exception:
        log.exception("title generation failed")
        return ""
    return _clean_title(title)


def _clean_title(raw):
    """Strip quotes, punctuation, and excess whitespace from an LLM title."""
    if not raw:
        return ""
    t = raw.strip()
    # Drop leading 'Title:' / 'Meeting Title:' if the model added one
    for prefix in ("Title:", "Meeting Title:", "TITLE:"):
        if t.lower().startswith(prefix.lower()):
            t = t[len(prefix):].strip()
    # Strip surrounding quotes / brackets / trailing periods
    t = t.strip("\"'`*_[]() \t")
    t = t.rstrip(".")
    # Collapse internal whitespace
    t = " ".join(t.split())
    return t[:80]  # hard cap


def cleanup_dictation(cfg, text):
    system = (
        "You clean up dictated text. Fix punctuation and capitalization and "
        "remove filler words (um, uh, you know). Keep the wording and meaning "
        "intact. Output only the cleaned text, with no preamble."
    )
    return llm.chat(cfg, system, text, max_tokens=1024)
