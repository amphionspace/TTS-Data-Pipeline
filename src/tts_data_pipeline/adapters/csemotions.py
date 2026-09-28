"""CSEMOTIONS: preserve emotion; no upstream utterance ID in inspected schema."""

from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config not in (None, "default"):
        raise ValueError("CSEMOTIONS only supports config=default")
    return sorted((root / "data").glob("*.parquet"))


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for shard, index, row in source_rows(root, files):
        yield make_record(
            dataset_id="csemotions",
            source_snapshot=snapshot,
            source_key=f"{shard}#row={index}",
            source_locator={"root": "raw_tts", "path": f"CSEMOTIONS/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=Path(row["audio"]["path"] or "audio.wav").name,
            text=row["text"] or None,
            text_kind="source_transcript" if row["text"] else None,
            language="zh",
            speaker_id=f"csemotions:{row['speaker']}",
            speaker_scope="dataset",
            source_split="train",
            source_config="default",
            metadata={
                "emotion": row["emotion"],
                "upstream_speaker": row["speaker"],
                "identity_method": "immutable_source_shard_row",
                "original_split": "train",
                "original_split_group": "train",
            },
        )
