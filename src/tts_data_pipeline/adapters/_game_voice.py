"""Shared mapping for character-labelled multilingual game recordings."""

from pathlib import Path
from urllib.parse import quote

from ..schema import make_record
from ._parquet import source_rows

LANGUAGES = {
    "Chinese(PRC)": "zh-CN",
    "Chinese": "zh",
    "English(US)": "en-US",
    "English": "en",
    "Japanese": "ja",
    "Korean": "ko",
    "": None,
    None: None,
}


def input_files(root, config=None):
    if config not in (None, "all", "default"):
        raise ValueError("Supported config: all/default")
    return sorted((root / "data").glob("*.parquet"))


def records(root, snapshot, files, dataset, directory):
    for shard, index, row in source_rows(root, files):
        raw_language = row["language"]
        if raw_language not in LANGUAGES:
            raise ValueError(f"Unknown source language: {raw_language!r}")
        language = LANGUAGES[raw_language]
        speaker = row["speaker"] if row["speaker"] and row["speaker"].strip() else None
        text = (
            row["transcription"] if row["transcription"] and row["transcription"].strip() else None
        )
        yield make_record(
            dataset_id=dataset,
            source_snapshot=snapshot,
            source_key=f"{shard}#row={index}",
            source_locator={"root": "raw_tts", "path": f"{directory}/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=Path(row["audio"]["path"] or "audio.wav").name,
            text=text,
            text_kind="source_transcript" if text else None,
            language=language,
            speaker_id=f"{dataset}:{language}:{quote(speaker, safe='')}"
            if speaker and language
            else None,
            speaker_scope="dataset" if speaker and language else None,
            source_config="default",
            metadata={
                "original_split": "train",
                "identity_method": "immutable_source_shard_row",
                "speaker_label_kind": "character_per_language",
                "upstream_audio_path": row["audio"]["path"],
                "upstream": {k: v for k, v in row.items() if k != "audio"},
            },
        )
