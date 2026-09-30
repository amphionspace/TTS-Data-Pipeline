"""In-memory file decoding; bitwise-validated system Opus and original other formats."""

import ctypes
import os

import numpy as np
import soundfile as sf

from .audio import InvalidAudio

DECODER_LIBRARIES = (
    "/usr/lib/x86_64-linux-gnu/libsndfile.so.1",
    "/usr/lib/x86_64-linux-gnu/libopus.so.0",
)

libc = ctypes.CDLL(None, use_errno=True)
libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
libc.memfd_create.restype = ctypes.c_int


class AudioInfo(ctypes.Structure):
    _fields_ = [
        ("frames", ctypes.c_int64),
        ("samplerate", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("format", ctypes.c_int),
        ("sections", ctypes.c_int),
        ("seekable", ctypes.c_int),
    ]


sndfile = ctypes.CDLL("/usr/lib/x86_64-linux-gnu/libsndfile.so.1")
sndfile.sf_open_fd.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(AudioInfo), ctypes.c_int]
sndfile.sf_open_fd.restype = ctypes.c_void_p
sndfile.sf_readf_float.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64]
sndfile.sf_readf_float.restype = ctypes.c_int64
sndfile.sf_error.argtypes = [ctypes.c_void_p]
sndfile.sf_error.restype = ctypes.c_int
sndfile.sf_close.argtypes = [ctypes.c_void_p]
sndfile.sf_close.restype = ctypes.c_int


def read_opus(raw):
    fd = libc.memfd_create(b"speaker-audio", 1)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "memfd_create failed")
    with os.fdopen(fd, "w+b") as stream:
        stream.write(raw)
        stream.seek(0)
        info = AudioInfo()
        handle = sndfile.sf_open_fd(stream.fileno(), 0x10, ctypes.byref(info), 0)
        if not handle:
            raise InvalidAudio("audio_decode_failed")
        try:
            audio = np.empty((info.frames, info.channels), dtype=np.float32)
            actual = sndfile.sf_readf_float(handle, audio.ctypes.data, info.frames)
            if actual < 0 or sndfile.sf_error(handle):
                raise InvalidAudio("audio_decode_failed")
            return audio[:actual], info.samplerate
        finally:
            sndfile.sf_close(handle)


def read_audio(raw):
    if raw[:4] == b"OggS" and len(raw) > 27:
        packet = 27 + raw[26]
        if raw[packet : packet + 8] == b"OpusHead":
            return read_opus(raw)
    fd = libc.memfd_create(b"speaker-audio", 1)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "memfd_create failed")
    with os.fdopen(fd, "w+b") as stream:
        stream.write(raw)
        stream.seek(0)
        try:
            return sf.read(stream.fileno(), dtype="float32", always_2d=True, closefd=False)
        except sf.LibsndfileError as exc:
            raise InvalidAudio("audio_decode_failed") from exc
