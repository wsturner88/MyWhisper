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


def _call_llm(cfg, system, user, max_tokens, label="", on_progress=None):
    """Call the LLM with one warm-up retry if the first response is empty.

    on_progress(text_delta, total_chars) is called as tokens stream in
    so the UI can show live progress. If on_progress is None, runs
    non-streaming (faster path).
    """
    on_token = None
    if on_progress is not None:
        # Wrap the per-token callback to also keep a running char count
        state = {"chars": 0}

        def on_token(delta):
            state["chars"] += len(delta)
            try:
                on_progress(delta, state["chars"])
            except Exception:
                pass

    reply = llm.chat(cfg, system, user, max_tokens=max_tokens, on_token=on_token)
    if reply and reply.strip():
        log.info("LLM %s: %d chars returned", label, len(reply))
        return reply
    log.warning("LLM %s: empty response, retrying after 2s warm-up", label)
    time.sleep(2)
    reply = llm.chat(cfg, system, user, max_tokens=max_tokens, on_token=on_token)
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
                         calendar_event=None, live_notes="",
                         on_stage=None):
    """Chunked summarization, with prompts shaped by the selected preset.

    `calendar_event` is an optional dict from calendar_lookup (title,
    attendees, organizer, notes) — fed into the prompt so the summary
    uses real names instead of 'Speaker 1'.

    `live_notes` is freeform text the user typed in the notes pad while
    the meeting was being recorded — names, corrections, context. It's
    treated as authoritative ground-truth and added to the prompt.

    `on_stage(stage_text, chars_so_far)` is called as progress updates
    arrive — e.g. on_stage('Chunk 2 of 5', 0), then on_stage('Chunk 2
    of 5', 247) as tokens stream in. Lets the UI show real-time
    progress instead of a blocking spinner.

    Returns (title, summary_md).
    """
    preset_id = preset_id or config.get_meeting_preset()
    preset = config.MEETING_PRESETS.get(preset_id, config.MEETING_PRESETS["general"])
    system = _system_prompt(preset)
    cal_block = calendar_lookup.context_block(calendar_event)
    notes_block = _user_notes_block(live_notes)

    def _stage(label):
        if on_stage:
            try:
                on_stage(label, 0)
            except Exception:
                pass

    def _stage_progress_factory(label):
        if on_stage is None:
            return None

        def _cb(_delta, total_chars):
            try:
                on_stage(label, total_chars)
            except Exception:
                pass
        return _cb

    chunks = _chunks(transcript_text)
    if not chunks:
        return "", "## Summary\n- (empty transcript)\n"

    if len(chunks) == 1:
        notes = chunks
    else:
        notes = []
        for i, chunk in enumerate(chunks, 1):
            stage_label = f"Summarizing part {i} of {len(chunks)}"
            _stage(stage_label)
            user = (
                f"This is part {i} of {len(chunks)} of a meeting transcript "
                f"for a **{preset['label']}**.\n\n"
                f"{cal_block}{notes_block}\n"
                f"Pull out {preset['focus']} from this part. Use the real "
                f"attendee names from the calendar context above instead of "
                f"'Speaker 1' / 'Speaker 2' where you can tell who spoke.\n\n"
                f"Transcript part:\n{chunk}"
            )
            notes.append(_call_llm(
                cfg, system, user, max_tokens=1024,
                label=f"chunk {i}/{len(chunks)}",
                on_progress=_stage_progress_factory(stage_label),
            ))

    combined = "\n\n".join(notes)
    _stage("Writing final summary")
    final = (
        f"Below are notes from a **{preset['label']}**.\n\n"
        f"{cal_block}{notes_block}\n"
        f"Produce final meeting notes in Markdown with exactly these sections:\n\n"
        "## Summary\n## Key Decisions\n## Action Items\n## Open Questions\n\n"
        f"Use bullet points. Use real attendee names from the calendar "
        f"context where possible. If the user's notes flag specific facts "
        f"(spellings, names, decisions), treat those as authoritative. If a "
        f"section has nothing, write '- None'. Lean into {preset['focus']}.\n\n"
        f"{combined}"
    )
    summary = _call_llm(
        cfg, system, final, max_tokens=2048, label="final pass",
        on_progress=_stage_progress_factory("Writing final summary"),
    )
    _stage("Generating title")
    title = _generate_title(cfg, summary, transcript_text)
    return title, summary


def _user_notes_block(live_notes):
    """Format the user's typed live notes for the LLM prompt."""
    if not live_notes or not live_notes.strip():
        return ""
    return (
        "### Notes from the user (typed during the meeting — authoritative)\n"
        f"{live_notes.strip()}\n\n"
    )


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
