"""Explicit, content-pinned rejection of individual zero-frame Parquet records."""

import hashlib
import io
import re
from pathlib import PurePosixPath

import soundfile as sf


def validate_record_exclusions(dataset, records):
    if records and dataset != "galgame":
        raise ValueError("Record exclusions are currently supported only for Galgame")
    seen = set()
    for item in records:
        path = PurePosixPath(item["path"])
        row = item["row"]
        if path.is_absolute() or ".." in path.parts or str(path) != item["path"]:
            raise ValueError("Record exclusion path must be a canonical source-relative path")
        if isinstance(row, bool) or not isinstance(row, int) or row < 0:
            raise ValueError("Record exclusion row must be a nonnegative integer")
        if (str(path), row) in seen or not item.get("reason"):
            raise ValueError("Record exclusions require unique locations and a reason")
        seen.add((str(path), row))
        for field in ("sha256", "audio_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", item[field]):
                raise ValueError("Record exclusion requires full SHA256 digests")
        if item["bytes"] <= 0 or item["audio_bytes"] <= 0 or not item["audio_ID"]:
            raise ValueError("Record exclusion requires source sizes and original audio ID")
        if item["condition"] != "zero_decoded_frames":
            raise ValueError("Unsupported record exclusion condition")


def verify_excluded_row(row, item):
    data = row["audio"]["bytes"]
    if (
        row["audio_ID"] != item["audio_ID"]
        or len(data) != item["audio_bytes"]
        or hashlib.sha256(data).hexdigest() != item["audio_sha256"]
    ):
        raise ValueError("Excluded record changed; review policy")
    with sf.SoundFile(io.BytesIO(data)) as audio:
        if audio.frames != 0 or len(audio.read(1)) != 0:
            raise ValueError("Excluded record is no longer zero-frame audio")
