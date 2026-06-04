"""Whisper transcription with hallucination guardrails.

Whisper's default decoding settings can spiral into single-token
repetition loops ('yeahyeahyeahyeah…', 'kukukuku…') when fed silence,
brief noise, or very short utterances. We harden against this in three
ways:

1. `condition_on_previous_text=False` — the single most effective
   knob. Stops Whisper from being conditioned on its own prior tokens,
   which is what allows runaway loops to form.
2. `compression_ratio_threshold=1.8` (default is 2.4) — discards a
   segment whose token stream compresses too well (a sign the same
   token is repeating).
3. `no_speech_threshold=0.65` (default 0.6) — slightly more aggressive
   silence rejection.
4. `temperature=(0.0, 0.2, 0.4, 0.6)` — fallback ladder. If a low
   temperature produces a bad output, Whisper retries hotter.

After decoding we also scan the final text for obvious repetition
(>40% of characters are a single 4-char substring repeating) and drop
it as a hallucination — anything that pattern-matches like that is
not real speech.
"""

import logging
import re
import threading

import mlx_whisper

from . import audio

log = logging.getLogger("mywhisper")

# One lock so two threads never load the model concurrently.
_lock = threading.Lock()


def _looks_like_hallucination(text):
    """Detect runaway repetition loops in Whisper output.

    Catches both 'kukukuku…' (loop from the start) and
    'ney.Michael kukukuku…' (real speech then runaway loop). Approach:
    look at the *tail* of the text where loops typically live, and ask
    'is some short cycle dominating this?'.
    """
    if not text:
        return False
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 40:
        return False  # too short to tell

    # Inspect the last 60% of the string — runaway loops manifest there.
    tail = stripped[max(0, len(stripped) // 2 - 10):]
    if len(tail) < 30:
        return False

    # Look for any 2- to 12-char cycle that covers most of the tail.
    for cycle in range(2, 13):
        if len(tail) < cycle * 6:
            continue
        # Try several anchor offsets so we don't depend on perfect alignment.
        for start in range(0, min(cycle * 3, len(tail) - cycle)):
            seed = tail[start:start + cycle]
            if not seed.strip("."):
                continue   # skip pure punctuation cycles
            repeats = tail.count(seed)
            covered = repeats * cycle
            if covered >= len(tail) * 0.65:
                return True
    return False


def transcribe_array(data, model, initial_prompt=None):
    """Transcribe a 16kHz mono float32 numpy array.

    initial_prompt biases Whisper toward custom vocabulary. Returns
    (segments, full_text); each segment has start/end/text keys.
    """
    kwargs = {
        "path_or_hf_repo": model,
        # Anti-hallucination guardrails — see module docstring.
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 1.8,
        "no_speech_threshold": 0.65,
        "temperature": (0.0, 0.2, 0.4, 0.6),
    }
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    with _lock:
        result = mlx_whisper.transcribe(data, **kwargs)
    segments = result.get("segments") or []
    text = (result.get("text") or "").strip()
    if _looks_like_hallucination(text):
        log.warning("transcribe: dropped probable hallucination loop: %r",
                    text[:80])
        return [], ""
    return segments, text


def transcribe_file(path, model):
    return transcribe_array(audio.load_mono_16k(path), model)


def prewarm(model):
    """Load the model into memory so the first real transcription is fast."""
    import numpy as np
    transcribe_array(np.zeros(16000, dtype="float32"), model)
