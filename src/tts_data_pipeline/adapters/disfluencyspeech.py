"""DisfluencySpeech (amaai-lab): one studio speaker re-reading Switchboard turns.

The text is the fully annotated transcript (Switchboard disfluency markup and <event> tags);
the three reduced transcripts are variants.
"""

import re
from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows

SPLITS = ("train", "validation", "test")
VARIANTS = (
    ("transcript_a", "source_transcript_no_events"),
    ("transcript_b", "source_transcript_no_fillers"),
    ("transcript_c", "source_transcript_no_false_starts"),
)


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config not in (None, "all", "default"):
        raise ValueError("DisfluencySpeech only supports config=default")
    files = sorted((root / "data").glob("*.parquet"))
    if {split_of(p.name) for p in files} != set(SPLITS):
        raise ValueError("DisfluencySpeech must have train, validation and test shards")
    return files


def split_of(name):
    match = re.fullmatch(r"(train|validation|test)-\d{5}-of-\d{5}\.parquet", name)
    if not match:
        raise ValueError(f"Unexpected DisfluencySpeech shard: {name}")
    return match.group(1)


def present(value):
    return value if value is not None and value.strip() else None


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for shard, index, row in source_rows(root, files):
        split = split_of(Path(shard).name)
        name = row["audio"]["path"]
        if not re.fullmatch(r"\d+\.wav", name or ""):
            raise ValueError(f"Unexpected DisfluencySpeech audio name: {name}")
        text = present(row["transcript_annotated"])
        yield make_record(
            dataset_id="disfluencyspeech",
            source_snapshot=snapshot,
            source_key=Path(name).stem,
            source_locator={"root": "raw_tts", "path": f"DisfluencySpeech/{shard}", "row": index},
            audio_bytes=row["audio"]["bytes"],
            audio_name=name,
            text=text,
            text_kind="source_transcript_annotated" if text else None,
            language="en",
            speaker_id="disfluencyspeech:speaker",
            speaker_scope="dataset",
            source_config="default",
            source_split="train",
            text_variants=[
                {"kind": kind, "text": row[column], "language": "en"}
                for column, kind in VARIANTS
                if present(row[column])
            ],
            metadata={"original_split": split, "original_split_group": split},
        )
