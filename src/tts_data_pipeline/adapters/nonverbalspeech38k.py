"""NonVerbalSpeech-38K: in-the-wild zh/en clips with one inline [nonverbal] tag, no speaker.

`caption` is the 2025-08-31 re-integration of the tag with the ASR text; the initial
`v1_caption` stays as a variant. Rows have no upstream ID, so identity is shard and row.
"""

import re
from pathlib import Path

from ..schema import make_record
from ._parquet import source_rows

LANGUAGES = {"ZH": "zh", "EN": "en"}


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config not in (None, "all", "default"):
        raise ValueError("NonVerbalSpeech-38K only supports config=default")
    files = sorted((root / "data").glob("*.parquet"))
    names = [re.fullmatch(r"train-(\d{5})-of-(\d{5})\.parquet", p.name) for p in files]
    if not names or None in names:
        raise ValueError("Unexpected NonVerbalSpeech-38K shard names")
    expected = int(names[0].group(2))
    if sorted(int(m.group(1)) for m in names) != list(range(expected)) or any(
        int(m.group(2)) != expected for m in names
    ):
        raise ValueError("NonVerbalSpeech-38K shard set is incomplete")
    return files


def present(value):
    return value.strip() if value is not None and value.strip() else None


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for shard, index, row in source_rows(root, files):
        if row["language"] not in LANGUAGES:
            raise ValueError(f"Unexpected NonVerbalSpeech-38K language: {row['language']}")
        language = LANGUAGES[row["language"]]
        text, initial = present(row["caption"]), present(row["v1_caption"])
        yield make_record(
            dataset_id="nonverbalspeech38k",
            source_snapshot=snapshot,
            source_key=f"{shard}#row={index}",
            source_locator={
                "root": "raw_tts",
                "path": f"NonVerbalSpeech-38K/{shard}",
                "row": index,
            },
            audio_bytes=row["audio"]["bytes"],
            audio_name=f"{Path(shard).stem}_{index:06d}.wav",
            text=text,
            text_kind="source_asr_paralinguistic" if text else None,
            language=language,
            source_config="default",
            source_split="train",
            text_variants=[
                {"kind": "source_asr_paralinguistic_v1", "text": initial, "language": language}
            ]
            if initial
            else [],
            metadata={
                "nonverbal_label": row["label"],
                "nonverbal_region": row["non_verbal_region"],
                "upstream_source": row["source"],
                "upstream_duration": row["duration"],
                "identity_method": "immutable_source_shard_row",
                "original_split": "train",
                "original_split_group": "train",
            },
        )
