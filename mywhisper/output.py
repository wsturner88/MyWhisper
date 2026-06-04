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


def save_meeting(out_dir, transcript_md, summary_md, title=""):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = _slug(title)
    filename = f"meeting_{stamp}_{slug}.md" if slug else f"meeting_{stamp}.md"
    path = Path(out_dir) / filename
    heading = title.strip() if title else f"Meeting Notes — {stamp}"
    path.write_text(
        f"# {heading}\n"
        f"_{stamp}_\n\n"
        f"{summary_md}\n\n"
        f"---\n\n## Full Transcript\n\n{transcript_md}\n"
    )
    return path
