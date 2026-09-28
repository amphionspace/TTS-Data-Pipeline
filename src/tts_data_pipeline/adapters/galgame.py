"""Galgame reupload: game configurations, original text and audio ID; no speaker inference."""

from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows


def input_files(root, config=None):
    files = sorted(root.glob("*/*.parquet"))
    if config not in (None, "all"):
        if config not in {p.parent.name for p in files}:
            raise ValueError(f"Unknown game config: {config}")
        files = [p for p in files if p.parent.name == config]
    return files


def iter_records(root, snapshot, *, files=None, excluded_records=()):
    for shard, index, row in source_rows(root, files, excluded_records):
        game = Path(shard).parent.name
        text = row["text"] if row["text"] and row["text"].strip() else None
        yield make_record(
            dataset_id="galgame",
            source_snapshot=snapshot,
            source_key=f"{shard}#row={index}",
            source_locator={
                "root": "raw_tts",
                "path": f"Galgame-VisualNovel-Reupload/{shard}",
                "row": index,
            },
            audio_bytes=row["audio"]["bytes"],
            audio_name=Path(row["audio"]["path"] or "audio.ogg").name,
            text=text,
            text_kind="source_transcript" if text else None,
            language="ja",
            source_config=game,
            group_id=f"galgame:game:{game}",
            metadata={
                "original_split": "train",
                "game": game,
                "upstream_audio_id": row["audio_ID"],
                "upstream_audio_path": row["audio"]["path"],
                "identity_method": "immutable_source_shard_row",
            },
        )
