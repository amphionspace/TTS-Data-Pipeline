"""HiFiTTS2 local 22.05 kHz FLAC Parquet export, preserving upstream metadata."""

import json
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import safe_member
from ._parquet import source_rows


def input_files(root, config=None):
    if config not in (None, "all"):
        raise ValueError("HiFiTTS2 accepts all local Parquet shards")
    files = sorted((root / "data").glob("shard-*.parquet"))
    if not files:
        raise ValueError("No HiFiTTS2 Parquet shards found")
    return files


def iter_records(root, snapshot, *, files=None):
    for shard, index, up in source_rows(root, files if files is not None else input_files(root)):
        key = safe_member(up["audio_filepath"])
        parts = PurePosixPath(key).parts
        speaker = up["speaker"]
        if len(parts) != 3 or parts[0] != speaker or not key.endswith(".flac"):
            raise ValueError(f"HiFiTTS2 speaker/path mismatch: {key}")
        if up["audio"]["path"] != key:
            raise ValueError(f"HiFiTTS2 embedded audio path mismatch: {key}")
        metadata = json.loads(up["metadata_json"])
        if not isinstance(metadata, dict):
            raise ValueError("HiFiTTS2 original metadata must be an object")
        scalars = {k: v for k, v in up.items() if k not in {"audio", "metadata_json"}}
        for field, value in scalars.items():
            if field in metadata and metadata[field] != value:
                raise ValueError(f"HiFiTTS2 conflicting metadata: {field}")
        text = up["text"] if up["text"] and up["text"].strip() else None
        normalized = up.get("normalized_text")
        row = make_record(
            dataset_id="hifitts2",
            source_snapshot=snapshot,
            source_key=key,
            source_locator={"root": "hifitts2_parquet", "path": shard, "row": index},
            audio_bytes=up["audio"]["bytes"],
            audio_name=parts[-1],
            text=text,
            text_kind="source_transcript" if text else None,
            text_variants=[{"kind": "source_normalized", "text": normalized, "language": "en"}]
            if normalized and normalized.strip()
            else [],
            language="en",
            speaker_id=f"hifitts2:{speaker}",
            speaker_scope="dataset",
            source_config="all",
            group_id=f"hifitts2:book:{parts[1]}",
            metadata={
                "original_split": up["set"],
                "upstream": metadata,
                "parquet_fields": scalars,
                "input_representation": "embedded_flac_v1_22050hz",
            },
        )
        if (row["sample_rate"], row["channels"]) != (22050, 1):
            raise ValueError(f"HiFiTTS2 expects 22050 Hz mono FLAC: {key}")
        if not up["audio"]["bytes"].startswith(b"fLaC"):
            raise ValueError(f"HiFiTTS2 expects FLAC bytes: {key}")
        yield row
