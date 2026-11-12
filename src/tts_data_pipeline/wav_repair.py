"""Repair of WAV files whose data chunk starts with an embedded Apple CAF file.

FlexiVoice kespeech WAVs were written as a RIFF header around a whole CAF file: the RIFF data
chunk begins with the CAF header (`caff`, `desc`, `free`, `data`, ...), and only after it come
the PCM samples. Every WAV reader decodes the header bytes as audio (a full-scale burst at 0 s
and near the CAF `data` tag). The repair skips exactly those bytes after checking that the CAF
description is the same PCM stream the RIFF header declares.
"""

import struct

CAF_LPCM = b"lpcm"
CAF_FLAG_FLOAT = 1
CAF_FLAG_LITTLE_ENDIAN = 2


def wav_data_offset(raw):
    """Byte offset of the RIFF/WAVE data chunk payload, or None."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(raw):
        tag, size = raw[pos : pos + 4], struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        if tag == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def embedded_caf_header(raw, sample_rate, channels):
    """Bytes of an embedded CAF header at the start of the WAV data, or 0 when there is none.

    Raises ValueError when a CAF header is present but does not describe 16-bit little-endian
    integer PCM at the WAV's own rate and channel count, or does not end on a frame boundary."""
    start = wav_data_offset(raw)
    if start is None or raw[start : start + 4] != b"caff":
        return 0
    pos, desc = start + 8, None  # 'caff', uint16 version, uint16 flags
    while pos + 12 <= len(raw):
        tag, size = raw[pos : pos + 4], struct.unpack(">q", raw[pos + 4 : pos + 12])[0]
        if tag == b"desc":
            desc = struct.unpack(">dIIIIII", raw[pos + 12 : pos + 12 + 32])
        if tag == b"data":
            header = pos + 12 + 4 - start  # chunk header, then uint32 edit count
            if desc is None:
                raise ValueError("Embedded CAF header without a desc chunk")
            rate, fmt, flags, packet_bytes, packet_frames, caf_channels, bits = desc
            if (
                rate != sample_rate
                or fmt != int.from_bytes(CAF_LPCM, "big")
                or flags & CAF_FLAG_FLOAT
                or not flags & CAF_FLAG_LITTLE_ENDIAN
                or bits != 16
                or caf_channels != channels
                or packet_frames != 1
                or packet_bytes != 2 * channels
                or header % packet_bytes
            ):
                raise ValueError(f"Embedded CAF stream differs from the WAV header: {desc}")
            return header
        if size < 0:
            break
        pos += 12 + size
    raise ValueError("Embedded CAF header without a data chunk")
