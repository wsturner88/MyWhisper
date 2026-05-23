import logging
import time

from . import calendar_lookup, config, llm

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


def summarize_transcript(cfg, transcript_text, preset_id=None,
                         calendar_event=None):
    """Chunked summarization, with prompts shaped by the selected preset.

    `calendar_event` is an optional dict from calendar_lookup; if provided
    its title/attendees/notes/organizer are fed into the prompts so the
    summary uses real names instead of 'Speaker 1'.

    Returns (title, summary_md). Calendar title (if any) wins over the
    LLM-generated one — the caller is responsible for that choice.
    """
    preset_id = preset_id or config.get_meeting_preset()
    preset = config.MEETING_PRESETS.get(preset_id, config.MEETING_PRESETS["general"])
    system = _system_prompt(preset)
    cal_block = calendar_lookup.context_block(calendar_event)

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
                f"for a **{preset['label']}**.\n\n"
                f"{cal_block}\n"
                f"Pull out {preset['focus']} from this part. Use the real "
                f"attendee names from the calendar context above instead of "
                f"'Speaker 1' / 'Speaker 2' where you can tell who spoke.\n\n"
                f"Transcript part:\n{chunk}"
            )
            notes.append(_call_llm(cfg, system, user, max_tokens=1024,
                                   label=f"chunk {i}/{len(chunks)}"))

    combined = "\n\n".join(notes)
    final = (
        f"Below are notes from a **{preset['label']}**.\n\n"
        f"{cal_block}\n"
        f"Produce final meeting notes in Markdown with exactly these sections:\n\n"
        "## Summary\n## Key Decisions\n## Action Items\n## Open Questions\n\n"
        f"Use bullet points. Use real attendee names from the calendar "
        f"context where possible. If a section has nothing, write "
        f"'- None'. Lean into {preset['focus']}.\n\n"
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
