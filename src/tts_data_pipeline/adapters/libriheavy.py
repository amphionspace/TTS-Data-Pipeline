"""LibriHeavy: keep book text and ASR separate; select one explicit config."""

from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows

CONFIGS = (
    "small",
    "medium",
    "large",
    "dev",
    "test_clean",
    "test_clean_large",
    "test_other",
    "test_other_large",
)


def input_files(root: Path, config: str | None = None) -> list[Path]:
    config = config or "small"
    if config not in CONFIGS:
        raise ValueError(f"Unknown LibriHeavy config: {config}; choose {CONFIGS}")
    return sorted((root / "default" / config).rglob("*.parquet"))


def iter_records(
    root: Path, snapshot: str, *, files: list[Path] | None = None, excluded_records=()
):
    for shard, index, row in source_rows(
        root, files if files is not None else input_files(root), excluded_records
    ):
        config = Path(shard).parts[1]
        text = row.get("text_original") or None
        asr = row.get("text_transcription") or None
        speaker = row.get("speaker_id")
        book = row.get("librivox_book_id")
        yield make_record(
            dataset_id="libriheavy",
            source_snapshot=snapshot,
            source_key=row["id"],
            source_locator={"root": "raw_tts", "path": f"libriheavy/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=Path(row["audio"]["path"]).name,
            text=text,
            text_kind="source_book_text" if text else None,
            language="en",
            speaker_id=f"libriheavy:{speaker}" if speaker is not None else None,
            speaker_scope="dataset" if speaker is not None else None,
            source_config=config,
            source_split="train",
            group_id=f"libriheavy:book:{book}" if book is not None else None,
            text_variants=[{"kind": "source_asr", "text": asr, "language": "en"}] if asr else [],
            metadata={
                "original_config": config,
                "original_split": "train",
                "original_split_group": "train",
                "librivox_book_id": book,
                "upstream_audio_path": row["audio"]["path"],
                "upstream_audio_duration": row.get("audio_duration"),
            },
        )
