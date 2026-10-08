"""Native audio IO: existing libsndfile formats, plus explicitly pinned AAC/M4A."""

import hashlib
import io
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf


class AudioDecodeError(ValueError):
    """Malformed individual audio, distinct from missing decoder configuration."""


def is_mp4(raw):
    return len(raw) >= 12 and raw[4:8] == b"ftyp"


def ffmpeg_path():
    path = Path(
        os.environ.get(
            "TTS_FFMPEG", str(Path(__file__).resolve().parents[2] / "cache/audio-tools/ffmpeg")
        )
    ).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("AAC requires the pinned FFmpeg executable; set TTS_FFMPEG")
    return path


@lru_cache(maxsize=4)
def _decoder_profile(path, size, mtime):
    with Path(path).open("rb") as f:
        sha = hashlib.file_digest(f, "sha256").hexdigest()
    version = subprocess.check_output([path, "-version"], text=True).splitlines()[0]
    return dict(
        backend="ffmpeg",
        version=version,
        executable_sha256=sha,
        edit_list="applied",
        output="native-rate/native-channels/float32",
        waveform="complete actual decode; no padding or implicit trim",
    )


def aac_profile():
    path = ffmpeg_path()
    s = path.stat()
    return _decoder_profile(str(path), s.st_size, s.st_mtime_ns)


def decode_file(source, output):
    """Seekable float WAV output keeps long carrier decoding off Python's heap."""
    command = [
        str(ffmpeg_path()),
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "pcm_f32le",
        "-threads",
        "1",
        "-rf64",
        "auto",
        "-y",
        str(output),
    ]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode or result.stderr.strip():
        raise AudioDecodeError(result.stderr.decode(errors="replace")[-2000:])


@lru_cache(maxsize=2)
def _read_mp4(raw, backend_hash):
    # Only standalone short IO uses this cache. Long carriers use decode_file + seek.
    with tempfile.TemporaryDirectory(prefix="tts-aac-") as folder:
        source, output = Path(folder) / "input.m4a", Path(folder) / "decoded.wav"
        source.write_bytes(raw)
        decode_file(source, output)
        audio, rate = sf.read(output, dtype="float32", always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise AudioDecodeError("empty_or_nonfinite_audio")
    audio.flags.writeable = False
    return audio, rate


def read_audio(raw):
    if is_mp4(raw):
        return _read_mp4(raw, aac_profile()["executable_sha256"])
    return sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)


def audio_info(raw):
    if is_mp4(raw):
        audio, rate = read_audio(raw)
        return SimpleNamespace(samplerate=rate, channels=audio.shape[1], frames=len(audio))
    return sf.info(io.BytesIO(raw))
