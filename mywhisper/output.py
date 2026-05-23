import re
from datetime import datetime
from pathlib import Path


def format_transcript(segments, diarized):
    if not segments:
        return "_(no speech detected)_"

    if not diarized:
        return " ".join(s.get("text", "").strip() for s in segments).strip()

    speaker_map = {}
    blocks = []
    for seg in segments:
        raw = seg.get("speaker") or "unknown"
        if raw not in speaker_map:
            speaker_map[raw] = f"Speaker {len(speaker_map) + 1}"
        name = speaker_map[raw]
        text = seg.get("text", "").strip()
        if not text:
            continue
        if blocks and blocks[-1][0] == name:
            blocks[-1][1].append(text)
        else:
            blocks.append((name, [text]))
    return "\n\n".join(f"**{name}:** {' '.join(parts)}" for name, parts in blocks)


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
