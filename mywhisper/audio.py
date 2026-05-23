from math import gcd

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

TARGET_SR = 16000


def to_mono_16k(data, sr):
    data = np.asarray(data, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    sr = int(sr)
    if sr != TARGET_SR:
        divisor = gcd(sr, TARGET_SR)
        data = resample_poly(data, TARGET_SR // divisor, sr // divisor)
    return np.asarray(data, dtype="float32")


def load_mono_16k(path):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return to_mono_16k(data, sr)


def mix(a, b):
    n = max(len(a), len(b))
    a = np.pad(a, (0, n - len(a)))
    b = np.pad(b, (0, n - len(b)))
    combined = a + b
    peak = float(np.max(np.abs(combined))) if len(combined) else 0.0
    if peak > 1.0:
        combined = combined / peak
    return np.asarray(combined, dtype="float32")


def save_16k(path, data):
    sf.write(path, np.asarray(data, dtype="float32"), TARGET_SR, subtype="PCM_16")
