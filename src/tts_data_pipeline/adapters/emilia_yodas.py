"""Emilia-YODAS: preserve full recording-scoped speaker labels and upstream scores."""

import re
from pathlib import Path

from ..schema import make_record
from ._emilia import LANGUAGES, archive_pairs, checked_metadata, input_files  # noqa: F401


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        relative = path.relative_to(root)
        language = relative.parts[0]
        for member, metadata_member, audio, meta in archive_pairs(path):
            key, text, speaker = checked_metadata(meta, "_id", member, language)
            match = re.fullmatch(rf"({language}_[A-Za-z0-9_-]{{11}})_W\d+", key)
            if match is None:
                raise ValueError(f"Unexpected Emilia-YODAS source ID: {key}")
            recording = match[1]
            if speaker is not None and not re.fullmatch(
                re.escape(recording) + r"_SPEAKER_\d+", speaker
            ):
                raise ValueError(f"Emilia-YODAS speaker belongs to another recording: {key}")
            recording_id = f"emilia_yodas:{recording}"
            yield make_record(
                dataset_id="emilia_yodas",
                source_snapshot=snapshot,
                source_key=key,
                source_locator={
                    "root": "raw_tts",
                    "path": f"Emilia-YODAS/{relative}",
                    "member": member,
                    "metadata_member": metadata_member,
                },
                audio_bytes=audio,
                audio_name=Path(member).name,
                text=text,
                text_kind="source_transcript" if text else None,
                language=LANGUAGES[language],
                speaker_id=f"emilia_yodas:{speaker}" if speaker else None,
                speaker_scope="source_recording" if speaker else None,
                recording_id=recording_id,
                group_id=recording_id,
                source_config=language,
                metadata={"upstream": meta, "original_config": language, "original_split": None},
            )
