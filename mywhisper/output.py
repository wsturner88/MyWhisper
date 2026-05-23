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


def save_meeting(out_dir, transcript_md, summary_md):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = Path(out_dir) / f"meeting_{stamp}.md"
    path.write_text(
        f"# Meeting Notes - {stamp}\n\n"
        f"{summary_md}\n\n"
        f"---\n\n## Full Transcript\n\n{transcript_md}\n"
    )
    return path
