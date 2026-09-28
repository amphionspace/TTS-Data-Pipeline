"""LibriTTS-R: use upstream ID and preserve both original and normalized text."""

from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config == "all":
        config = None
    configs = {p.name for p in (root / "data").iterdir() if p.is_dir()}
    if config is not None and config not in configs:
        raise ValueError(f"Unknown LibriTTS-R config: {config}; choose {sorted(configs)}")
    directory = root / "data" / config if config else root / "data"
    return sorted(directory.rglob("*.parquet"))


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for shard, index, row in source_rows(root, files):
        config = Path(shard).parent.name
        yield make_record(
            dataset_id="libritts_r",
            source_snapshot=snapshot,
            source_key=row["id"],
            source_locator={"root": "raw_tts", "path": f"LibriTTS-R/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=f"{row['id']}.wav",
            text=row["text_original"],
            text_kind="source_transcript",
            language="en",
            speaker_id=f"libritts_r:{row['speaker_id']}",
            speaker_scope="dataset",
            source_config="all",
            source_split="train",
            recording_id=f"libritts_r:{row['speaker_id']}:{row['chapter_id']}",
            group_id=f"libritts_r:{row['speaker_id']}:{row['chapter_id']}",
            text_variants=[
                {"kind": "source_normalized", "text": row["text_normalized"], "language": "en"}
            ],
            metadata={
                "upstream_path": row["path"],
                "chapter_id": row["chapter_id"],
                "original_split": config,
                "original_split_group": config.split(".")[0],
            },
        )
