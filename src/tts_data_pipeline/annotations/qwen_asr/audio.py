"""Complete decoding and resampling; the service owns long-audio handling."""

import io
import math

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


def decode(data):
    wave, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    if not len(wave) or not np.isfinite(wave).all():
        raise ValueError("empty_or_nonfinite_audio")
    return wave.mean(axis=1, dtype=np.float32), rate, wave.shape[1]


def encode_request(wave, rate):
    factor = math.gcd(rate, 16000)
    converted = (
        resample_poly(wave, 16000 // factor, rate // factor).astype(np.float32)
        if rate != 16000
        else wave
    )
    buf = io.BytesIO()
    sf.write(buf, converted, 16000, format="WAV", subtype="FLOAT")
    return buf.getvalue(), len(converted)
