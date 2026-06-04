"""Whisper transcription with hallucination guardrails.

Whisper has two failure modes we have to defend against:

1. **Repetition loops** — fed silence or brief noise, the decoder can
   spiral into single-token loops ('kukukuku…', 'yeahyeahyeah…'). Even
   worse, this can happen *inside* a longer transcript where one
   segment runs away while the others are fine.

2. **Language drift to Japanese** — when Whisper is uncertain, its
   multilingual decoder has a strong bias toward Japanese (especially
   the YouTube outro 'ご視聴ありがとうございました'). On English meeting
   audio with quiet stretches, you end up with real English mixed with
   Japanese garbage.

We address both:

- `language='en'` forces English decoding. This single setting kills
  the Japanese drift entirely — the decoder never produces Japanese
  tokens to begin with.
- `condition_on_previous_text=False` stops repetition loops from
  feeding themselves token-to-token across the audio.
- `compression_ratio_threshold=1.8` (default 2.4) rejects segments
  whose token stream compresses too well.
- `no_speech_threshold=0.65` (default 0.6) drops more silence early.
- `temperature=(0.0, 0.2, 0.4, 0.6)` retries with a hotter sample if
  the cold one looked bad.

After decoding, every segment is filtered individually for repetition
and for CJK content. Bad segments are dropped; the surviving segments
are joined into the final transcript.
"""

import logging
import re
import threading

import mlx_whisper

from . import audio, config

log = logging.getLogger("mywhisper")

_lock = threading.Lock()


# ---------- per-segment quality checks --------------------------------------

# Unicode blocks for the languages Whisper most often hallucinates into
# when it can't parse English audio.
_CJK_RANGES = (
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xAC00, 0xD7AF),   # Korean Hangul Syllables
    (0xFF66, 0xFF9F),   # Halfwidth Katakana
)


def _is_cjk(ch):
    code = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= code <= hi:
            return True
    return False


def _looks_like_non_english(text):
    """Returns True if the text is dominated by CJK characters —
    almost always means Whisper drifted into Japanese hallucinations
    on a chunk of English meeting audio. Even 10% is suspicious; the
    real meeting was in English."""
    if not text:
        return False
    cjk = sum(1 for c in text if _is_cjk(c))
    return cjk >= 5 and cjk / len(text) > 0.10


def _looks_like_repetition(text):
    """Catches both 'kukukuku…' loops and 'Jim Jim Jim Jim…' loops."""
    if not text:
        return False
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 24:
        return False
    # Look anywhere in the string for a 2–12-char cycle that dominates.
    for cycle in range(2, 13):
        if len(stripped) < cycle * 5:
            continue
        for start in range(0, min(cycle * 3, len(stripped) - cycle)):
            seed = stripped[start:start + cycle]
            if not seed.strip("."):
                continue
            repeats = stripped.count(seed)
            if repeats * cycle >= len(stripped) * 0.55:
                return True
    return False


def _segment_is_bad(seg_text):
    """A segment is dropped if it looks like a hallucination."""
    if not seg_text:
        return True
    if _looks_like_non_english(seg_text):
        return "non-english"
    if _looks_like_repetition(seg_text):
        return "repetition"
    return False


# ---------- public API ------------------------------------------------------


def _language():
    """Read the active Whisper language from config. Defaults to 'en'."""
    try:
        cfg = config.load()
        lang = (cfg.get("whisper", {}).get("language") or "en").strip()
        return lang or "en"
    except Exception:
        return "en"


def transcribe_array(data, model, initial_prompt=None):
    """Transcribe a 16kHz mono float32 numpy array.

    Returns (segments, full_text). initial_prompt biases Whisper toward
    custom vocabulary. Bad segments (hallucinated Japanese, runaway
    repetition) are filtered out before the text is built.
    """
    kwargs = {
        "path_or_hf_repo": model,
        "language": _language(),       # forces English decoder by default
        "task": "transcribe",          # never translate
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 1.8,
        "no_speech_threshold": 0.65,
        "temperature": (0.0, 0.2, 0.4, 0.6),
    }
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt

    with _lock:
        result = mlx_whisper.transcribe(data, **kwargs)

    raw_segments = result.get("segments") or []
    good = []
    drops = {"non-english": 0, "repetition": 0, "empty": 0}
    for seg in raw_segments:
        seg_text = (seg.get("text") or "").strip()
        reason = _segment_is_bad(seg_text)
        if reason is True:
            drops["empty"] += 1
            continue
        if reason:
            drops[reason] += 1
            log.info("transcribe: dropped %s segment: %r",
                     reason, seg_text[:80])
            continue
        good.append(seg)

    total_dropped = sum(drops.values())
    if total_dropped:
        log.warning(
            "transcribe: kept %d/%d segments (dropped %d non-english, "
            "%d repetition, %d empty)",
            len(good), len(raw_segments),
            drops["non-english"], drops["repetition"], drops["empty"],
        )

    text = " ".join(s.get("text", "").strip() for s in good).strip()
    text = re.sub(r"\s+", " ", text)
    return good, text


_MIN_CHANNEL_SAMPLES = 4800  # 0.3s at 16kHz


def transcribe_meeting(mic_data, sys_data, model, initial_prompt=None,
                       on_stage=None):
    """Dual-channel meeting transcription.

    Transcribes the microphone ('Me') and the system-audio ('Others')
    streams SEPARATELY, then merges the segments by timestamp. This is
    the key fix for the garbled / hallucinated transcripts that appeared
    once Teams system audio was mixed into the mic: each stream is now a
    single clean source, so Whisper has far less mush to hallucinate on,
    and we get perfect speaker separation (you vs. everyone else) for
    free — no audio mixing, no clock-drift, no overlap.

    Returns (segments, speaker_labeled):
      - segments: list of {start, end, text, speaker} sorted by start
      - speaker_labeled: True if we have Me/Others labels (system audio
        was present), False for a plain mic-only run.
    """
    def stage(msg):
        if on_stage:
            try:
                on_stage(msg)
            except Exception:
                pass

    stage("Transcribing your microphone…")
    mic_segs, _ = transcribe_array(mic_data, model, initial_prompt)
    for s in mic_segs:
        s["speaker"] = "Me"

    has_sys = sys_data is not None and len(sys_data) >= _MIN_CHANNEL_SAMPLES
    if not has_sys:
        # Mic-only. Not speaker-labeled here — the caller may still run
        # diarization to separate people in the room.
        return mic_segs, False

    stage("Transcribing the call audio…")
    sys_segs, _ = transcribe_array(sys_data, model, initial_prompt)
    for s in sys_segs:
        s["speaker"] = "Others"

    merged = sorted(mic_segs + sys_segs,
                    key=lambda s: float(s.get("start") or 0.0))
    log.info("transcribe_meeting: %d mic + %d call = %d merged segments",
             len(mic_segs), len(sys_segs), len(merged))
    return merged, True


def transcribe_file(path, model):
    return transcribe_array(audio.load_mono_16k(path), model)


def prewarm(model):
    import numpy as np
    transcribe_array(np.zeros(16000, dtype="float32"), model)
