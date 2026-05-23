import threading

import mlx_whisper

from . import audio

# Serializes transcription so the model is never loaded by two threads at once.
_lock = threading.Lock()


def transcribe_array(data, model, initial_prompt=None):
    """Transcribe a 16kHz mono float32 numpy array.

    initial_prompt biases Whisper toward custom vocabulary. Returns
    (segments, full_text); each segment has start/end/text keys.
    """
    kwargs = {"path_or_hf_repo": model}
    if initial_prompt:
        kwargs["initial_prompt"] = initial_prompt
    with _lock:
        result = mlx_whisper.transcribe(data, **kwargs)
    segments = result.get("segments") or []
    return segments, (result.get("text") or "").strip()


def transcribe_file(path, model):
    return transcribe_array(audio.load_mono_16k(path), model)


def prewarm(model):
    """Load the model into memory so the first real transcription is fast."""
    import numpy as np
    transcribe_array(np.zeros(16000, dtype="float32"), model)
