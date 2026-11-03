"""NonverbalTTS (deepvk): English VoxCeleb/Expresso clips with emoji nonverbal tags in text.

`Result` is the upstream fusion of three human annotations and keeps the inline NV emoji;
the ASR hypothesis and every annotator response stay as variants or metadata.
"""

from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows

SPLITS = ("train", "dev", "test", "other")


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config not in (None, "all", "default"):
        raise ValueError("NonverbalTTS only supports config=default")
    found = {p.name for p in (root / "default").iterdir() if p.is_dir()}
    if found != set(SPLITS):
        raise ValueError(f"Unexpected NonverbalTTS splits: {sorted(found)}")
    return sorted((root / "default").rglob("*.parquet"))


def text_or_none(value):
    return value if value is not None and value.strip() else None


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for shard, index, row in source_rows(root, files):
        split = Path(shard).parent.name
        if split not in SPLITS:
            raise ValueError(f"NonverbalTTS row outside a known split: {shard}")
        text, initial = text_or_none(row["Result"]), text_or_none(row["Initial text"])
        yield make_record(
            dataset_id="nonverbaltts",
            source_snapshot=snapshot,
            source_key=row["index"],
            source_locator={"root": "raw_tts", "path": f"NonverbalTTS/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=Path(row["audio"]["path"]).name,
            text=text,
            text_kind="source_transcript_paralinguistic" if text else None,
            language="en",
            speaker_id=f"nonverbaltts:{row['speaker_id']}",
            speaker_scope="dataset",
            source_config="default",
            source_split="train",
            text_variants=[{"kind": "source_asr_paralinguistic", "text": initial, "language": "en"}]
            if initial
            else [],
            metadata={
                "emotion": row["Emotion"],
                "annotator_responses": [row[f"Annotator response {i}"] for i in (1, 2, 3)],
                "dnsmos": row["dnsmos"],
                "upstream_duration": row["duration"],
                "upstream_speaker": row["speaker_id"],
                "upstream_corpus": row["data_name"],
                "gender": row["gender"],
                "original_split": split,
                "original_split_group": split,
            },
        )
