from . import config

_MODEL = "pyannote/speaker-diarization-3.1"
_pipeline = None


def available():
    return bool(config.get_secret("hf_token"))


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from pyannote.audio import Pipeline

        token = config.get_secret("hf_token")
        if not token:
            raise RuntimeError("Hugging Face token not set. Run: ./run.sh setup")
        try:
            _pipeline = Pipeline.from_pretrained(_MODEL, use_auth_token=token)
        except TypeError:  # newer pyannote renamed the argument
            _pipeline = Pipeline.from_pretrained(_MODEL, token=token)
    return _pipeline


def diarize(wav_path):
    """Return a list of (start, end, speaker_label) turns."""
    annotation = _get_pipeline()(wav_path)
    return [
        (turn.start, turn.end, speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]


def label_segments(segments, turns):
    """Tag each transcript segment with the speaker it overlaps most."""

    def speaker_for(seg):
        start, end = seg.get("start", 0.0), seg.get("end", 0.0)
        best, best_overlap = None, 0.0
        for turn_start, turn_end, speaker in turns:
            overlap = min(end, turn_end) - max(start, turn_start)
            if overlap > best_overlap:
                best, best_overlap = speaker, overlap
        return best

    return [{**seg, "speaker": speaker_for(seg)} for seg in segments]
