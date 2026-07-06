import re
from datetime import datetime
from pathlib import Path


def _speaker_blocks(segments):
    """Group consecutive same-speaker segments into (name, [texts]) blocks.

    Friendly labels ('Me', 'Others') are kept as-is. Raw diarization
    labels (e.g. 'SPEAKER_00') are remapped to 'Speaker 1', 'Speaker 2'…
    in first-appearance order.
    """
    blocks = []
    mapping = {}
    for seg in segments:
        raw = seg.get("speaker") or "Unknown"
        if raw in ("Me", "Others"):
            name = raw
        else:
            if raw not in mapping:
                mapping[raw] = f"Speaker {len(mapping) + 1}"
            name = mapping[raw]
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if blocks and blocks[-1][0] == name:
            blocks[-1][1].append(text)
        else:
            blocks.append((name, [text]))
    return blocks


def format_transcript(segments, speaker_labeled):
    """Markdown transcript. With speaker_labeled, prefixes each block with
    a bold speaker name; otherwise a plain run of text."""
    if not segments:
        return "_(no speech detected)_"
    if not speaker_labeled:
        text = " ".join((s.get("text") or "").strip() for s in segments).strip()
        return text or "_(no speech detected)_"
    blocks = _speaker_blocks(segments)
    if not blocks:
        return "_(no speech detected)_"
    return "\n\n".join(f"**{name}:** {' '.join(parts)}" for name, parts in blocks)


def attributed_text(segments, speaker_labeled):
    """Plain speaker-attributed text for the LLM prompt — 'Me: …' /
    'Others: …' lines. Without speaker_labeled, a plain run of text."""
    if not segments:
        return ""
    if not speaker_labeled:
        return " ".join((s.get("text") or "").strip() for s in segments).strip()
    blocks = _speaker_blocks(segments)
    return "\n".join(f"{name}: {' '.join(parts)}" for name, parts in blocks)


def _slug(title, max_len=60):
    """Make a filename-safe slug from an LLM title."""
    if not title:
        return ""
    # Replace chars macOS / common filesystems disallow with a hyphen,
    # so '1:1 with John' becomes '1-1_with_John' not '11_with_John'.
    s = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", title)
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._-")
    return s[:max_len]


def save_meeting(out_dir, transcript_md, summary_md, title="", live_notes=""):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = _slug(title)
    filename = f"meeting_{stamp}_{slug}.md" if slug else f"meeting_{stamp}.md"
    path = Path(out_dir) / filename
    heading = title.strip() if title else f"Meeting Notes — {stamp}"
    # Whatever was typed into the floating notes pad is preserved
    # verbatim, between the summary and the transcript.
    notes_md = (live_notes or "").strip()
    notes_section = (
        f"---\n\n## My Notes (typed during the meeting)\n\n{notes_md}\n\n"
        if notes_md else ""
    )
    path.write_text(
        f"# {heading}\n"
        f"_{stamp}_\n\n"
        f"{summary_md}\n\n"
        f"{notes_section}"
        f"---\n\n## Full Transcript\n\n{transcript_md}\n"
    )
    return path


_TRANSCRIPT_HDR = "## Full Transcript"
_NOTES_HDR = "## My Notes (typed during the meeting)"


def parse_meeting(path):
    """Split a saved meeting file back into its parts so the summary can
    be regenerated from the transcript. Returns a dict with title, stamp,
    summary_md, notes_md, transcript_md (any of which may be '')."""
    raw = Path(path).read_text()

    title = ""
    m = re.search(r"^# (.+)$", raw, re.M)
    if m:
        title = m.group(1).strip()

    stamp = ""
    m = re.search(r"^_(.+)_\s*$", raw, re.M)   # the italic date line
    if m:
        stamp = m.group(1).strip()

    # Transcript: everything after the (last) Full Transcript header.
    transcript = ""
    ti = raw.rfind(_TRANSCRIPT_HDR)
    if ti != -1:
        transcript = raw[ti + len(_TRANSCRIPT_HDR):].strip()

    # Notes: between the notes header and the following '---' rule.
    notes = ""
    ni = raw.find(_NOTES_HDR)
    if ni != -1:
        nstart = ni + len(_NOTES_HDR)
        nend = raw.find("\n---", nstart)
        notes = raw[nstart:(nend if nend != -1 else len(raw))].strip()

    # Summary: between the stamp line and the first '---' rule. Only used
    # to preserve any leading banner; a stray '---' in the body is
    # harmless because the banner sits at the very top.
    summary = ""
    if stamp:
        si = raw.find(f"_{stamp}_")
        if si != -1:
            sstart = si + len(stamp) + 2
            send = raw.find("\n---", sstart)
            summary = raw[sstart:(send if send != -1 else len(raw))].strip()

    return {"title": title, "stamp": stamp, "summary_md": summary,
            "notes_md": notes, "transcript_md": transcript}


def rewrite_meeting(path, title, stamp, summary_md, notes_md, transcript_md):
    """Rewrite a meeting file in place with a new summary, keeping the
    same filename, title, date, notes, and transcript."""
    notes_section = (
        f"---\n\n{_NOTES_HDR}\n\n{notes_md}\n\n" if notes_md else ""
    )
    heading = title.strip() if title else f"Meeting Notes — {stamp}"
    Path(path).write_text(
        f"# {heading}\n"
        f"_{stamp}_\n\n"
        f"{summary_md}\n\n"
        f"{notes_section}"
        f"---\n\n{_TRANSCRIPT_HDR}\n\n{transcript_md}\n"
    )
    return Path(path)


def rename_meeting(path, title):
    """Rename a saved meeting file so its name carries the title slug —
    used when the title only becomes known (from the LLM) after the file
    was already written. Keeps the date/time stamp. Returns the new path,
    or the original if renaming isn't possible."""
    path = Path(path)
    m = re.match(r"(meeting_\d{4}-\d{2}-\d{2}_\d{4})", path.stem)
    slug = _slug(title)
    if not m or not slug:
        return path
    new = path.with_name(f"{m.group(1)}_{slug}.md")
    if new == path or new.exists():
        return path
    path.rename(new)
    return new


def transcript_to_attributed(transcript_md):
    """Convert a stored markdown transcript ('**Me:** …') back to the
    plain attributed form the summarizer expects ('Me: …')."""
    return re.sub(r"\*\*(.+?):\*\*", r"\1:", transcript_md or "").strip()
