"""v0.1 base contract. Arrow representation is separate from validation rules."""

import hashlib
import io
import json

import pyarrow as pa
import soundfile as sf

VERSION = "v0.1"
IDENTITY_SCHEME = "source-file-v1"


def digest(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def sample_id(dataset_id: str, source_snapshot: str, source_key: str) -> str:
    """No absolute paths, transcript, speaker label, or output row number in identity."""
    if not all(isinstance(v, str) and v.strip() for v in (dataset_id, source_snapshot, source_key)):
        raise ValueError("Identity components must be nonempty strings")
    return digest([dataset_id, source_snapshot, source_key])


def source_file_snapshot(relative_path: str, sha256: str) -> str:
    """Version one self-contained source file, independently of scheduler batches."""
    return f"{IDENTITY_SCHEME}:sha256:" + digest({"path": relative_path, "sha256": sha256})


def base_schema() -> pa.Schema:
    strings = [
        "schema_version",
        "sample_id",
        "record_revision",
        "dataset_id",
        "source_snapshot",
        "source_key",
        "source_config",
        "source_split",
        "source_locator_json",
        "recording_id",
        "group_id",
        "parent_sample_id",
        "audio_sha256",
        "text",
        "text_kind",
        "language",
        "speaker_id",
        "speaker_scope",
        "metadata_json",
    ]
    return pa.schema(
        [
            *[pa.field(name, pa.string()) for name in strings],
            pa.field(
                "audio",
                pa.struct([pa.field("bytes", pa.large_binary()), pa.field("path", pa.string())]),
            ),
            pa.field("sample_rate", pa.int32()),
            pa.field("channels", pa.int16()),
            pa.field("num_frames", pa.int64()),
            pa.field("duration_seconds", pa.float64()),
            pa.field("segment_start_frame", pa.int64()),
            pa.field("segment_end_frame", pa.int64()),
            pa.field(
                "text_variants",
                pa.list_(
                    pa.struct(
                        [
                            pa.field("kind", pa.string()),
                            pa.field("text", pa.string()),
                            pa.field("language", pa.string()),
                        ]
                    )
                ),
            ),
        ]
    )


BASE_FIELD_NAMES = tuple(base_schema().names)
BASE_FIELD_SET = frozenset(BASE_FIELD_NAMES)


def record_revision(row: dict) -> str:
    # Ignore audio display filename. Bytes are represented by their content hash.
    return digest({k: row[k] for k in BASE_FIELD_NAMES if k not in {"audio", "record_revision"}})


def make_record(
    *,
    dataset_id: str,
    source_snapshot: str,
    source_key: str,
    audio_bytes: bytes,
    audio_name: str,
    source_locator: dict,
    text: str | None = None,
    text_kind: str | None = None,
    language: str | None = None,
    speaker_id: str | None = None,
    speaker_scope: str | None = None,
    source_config: str | None = None,
    source_split: str = "train",
    recording_id: str | None = None,
    group_id: str | None = None,
    text_variants: list | None = None,
    metadata: dict | None = None,
) -> dict:
    info = sf.info(io.BytesIO(audio_bytes))
    row = dict.fromkeys(BASE_FIELD_NAMES)
    row.update(
        schema_version=VERSION,
        dataset_id=dataset_id,
        source_snapshot=source_snapshot,
        source_key=source_key,
        sample_id=sample_id(dataset_id, source_snapshot, source_key),
        source_locator_json=json.dumps(
            source_locator, ensure_ascii=False, sort_keys=True, allow_nan=False
        ),
        source_config=source_config,
        source_split=source_split,
        recording_id=recording_id,
        group_id=group_id,
        audio={"bytes": audio_bytes, "path": audio_name},
        audio_sha256=hashlib.sha256(audio_bytes).hexdigest(),
        sample_rate=info.samplerate,
        channels=info.channels,
        num_frames=info.frames,
        duration_seconds=info.frames / info.samplerate,
        text=text,
        text_kind=text_kind,
        language=language,
        speaker_id=speaker_id,
        speaker_scope=speaker_scope,
        text_variants=text_variants or [],
        metadata_json=json.dumps(
            metadata or {}, ensure_ascii=False, sort_keys=True, allow_nan=False
        ),
    )
    row["record_revision"] = record_revision(row)
    validate_record(row)
    return row


def validate_record(row: dict) -> None:
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    require(row.keys() == BASE_FIELD_SET, "Unexpected or missing base fields")
    require(row["schema_version"] == VERSION, "Unsupported schema version")
    require(row["source_split"] == "train", "Unified source_split must be train")
    require(
        row["sample_id"] == sample_id(row["dataset_id"], row["source_snapshot"], row["source_key"]),
        "Invalid sample identity",
    )
    data = row["audio"]["bytes"]
    require(isinstance(data, bytes) and len(data) > 0, "Embedded audio bytes required")
    require(hashlib.sha256(data).hexdigest() == row["audio_sha256"], "Audio hash mismatch")
    info = sf.info(io.BytesIO(data))
    require(
        (info.samplerate, info.channels, info.frames)
        == (row["sample_rate"], row["channels"], row["num_frames"]),
        "Audio header mismatch",
    )
    require(info.frames > 0, "Empty audio")
    require(
        abs(row["duration_seconds"] - info.frames / info.samplerate) < 1e-9, "Duration mismatch"
    )
    for key in ("text", "text_kind", "language", "speaker_id", "speaker_scope"):
        require(
            row[key] is None or (isinstance(row[key], str) and row[key].strip()),
            f"Use null for unknown {key}",
        )
    require((row["text"] is None) == (row["text_kind"] is None), "Text requires text_kind")
    require(
        (row["speaker_id"] is None) == (row["speaker_scope"] is None),
        "Speaker requires an identity scope",
    )
    variants = row["text_variants"]
    require(isinstance(variants, list), "text_variants must be a list")
    for variant in variants:
        require(
            isinstance(variant, dict) and variant.keys() == {"kind", "text", "language"},
            "Invalid text variant fields",
        )
        for key in ("kind", "text"):
            require(
                isinstance(variant[key], str) and bool(variant[key].strip()),
                "Text variant kind/text must be nonempty",
            )
        require(
            variant["language"] is None
            or (isinstance(variant["language"], str) and bool(variant["language"].strip())),
            "Use null for unknown variant language",
        )
    for key in ("source_locator_json", "metadata_json"):
        parsed = json.loads(row[key])
        require(isinstance(parsed, dict), f"{key} must encode an object")
        json.dumps(parsed, allow_nan=False)  # Reject NaN/Infinity, including overflow like 1e999.

    start, end = row["segment_start_frame"], row["segment_end_frame"]
    require((start is None) == (end is None), "Incomplete segment bounds")
    if start is not None:
        require(
            row["parent_sample_id"] is not None and 0 <= start < end,
            "Segment requires parent and ordered native frame bounds",
        )
    require(row["record_revision"] == record_revision(row), "Record revision mismatch")
