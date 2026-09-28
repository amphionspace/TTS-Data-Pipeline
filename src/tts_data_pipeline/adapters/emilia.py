"""Emilia: original MP3, transcript, complete source speaker label and metadata."""

import re
from pathlib import Path

from ..schema import make_record
from ._emilia import LANGUAGES, archive_pairs, checked_metadata, input_files  # noqa: F401


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        relative = path.relative_to(root)
        language = relative.parts[0]
        for member, metadata_member, audio, meta in archive_pairs(path):
            key, text, speaker = checked_metadata(meta, "id", member, language)
            if not re.fullmatch(rf"{language}_B\d+_S\d+_W\d+", key):
                raise ValueError(f"Unexpected Emilia source ID: {key}")
            if speaker is not None and speaker != key.rsplit("_W", 1)[0]:
                raise ValueError(f"Emilia speaker does not match source ID: {key}")
            if meta.get("wav") is not None and Path(meta["wav"]).name != Path(member).name:
                raise ValueError(f"Emilia wav basename does not match member: {member}")
            speaker_id = f"emilia:{speaker}" if speaker else None
            yield make_record(
                dataset_id="emilia",
                source_snapshot=snapshot,
                source_key=key,
                source_locator={
                    "root": "raw_tts",
                    "path": f"Emilia/{relative}",
                    "member": member,
                    "metadata_member": metadata_member,
                },
                audio_bytes=audio,
                audio_name=Path(member).name,
                text=text,
                text_kind="source_transcript" if text else None,
                language=LANGUAGES[language],
                speaker_id=speaker_id,
                speaker_scope="source_speaker_label" if speaker else None,
                group_id=speaker_id,
                source_config=language,
                metadata={"upstream": meta, "original_config": language, "original_split": None},
            )
