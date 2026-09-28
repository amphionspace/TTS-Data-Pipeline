"""Explicit, content-pinned rejection of individually reviewed Parquet records."""

import hashlib
import io
import re
from pathlib import PurePosixPath

import soundfile as sf


def validate_record_exclusions(dataset, records):
    id_fields = {"galgame": "audio_ID", "libriheavy": "id"}
    if records and dataset not in id_fields:
        raise ValueError("Record exclusions are supported only for Galgame and LibriHeavy")
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
        id_field = id_fields[dataset]
        if set(item) & {"audio_ID", "id"} != {id_field}:
            raise ValueError("Record exclusion requires the dataset's original ID field")
        if item["bytes"] <= 0 or item["audio_bytes"] <= 0 or not item[id_field]:
            raise ValueError("Record exclusion requires source sizes and original audio ID")
        if item["condition"] not in {
            "zero_decoded_frames",
            "sndfile_malformed",
            "sndfile_unrecognised",
        }:
            raise ValueError("Unsupported record exclusion condition")


def verify_excluded_row(row, item):
    data = row["audio"]["bytes"]
    id_field = "audio_ID" if "audio_ID" in item else "id"
    if (
        row[id_field] != item[id_field]
        or len(data) != item["audio_bytes"]
        or hashlib.sha256(data).hexdigest() != item["audio_sha256"]
    ):
        raise ValueError("Excluded record changed; review policy")
    error_codes = {"sndfile_malformed": 3, "sndfile_unrecognised": 1}
    if item["condition"] in error_codes:
        try:
            with sf.SoundFile(io.BytesIO(data)):
                pass
        except sf.LibsndfileError as exc:
            # Match the reviewed error exactly; other decoder errors must still fail.
            if exc.code != error_codes[item["condition"]]:
                raise ValueError("Excluded record has a different decoder error") from exc
            return
        raise ValueError(f"Excluded record is no longer rejected as {item['condition']}")
    if item["condition"] != "zero_decoded_frames":
        raise ValueError("Unsupported record exclusion condition")
    with sf.SoundFile(io.BytesIO(data)) as audio:
        if audio.frames != 0 or len(audio.read(1)) != 0:
            raise ValueError("Excluded record is no longer zero-frame audio")
