"""VCTK 0.92: both microphones, common utterance group, missing text retained."""

import re
import zipfile
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import inputs, safe_member

AUDIO = re.compile(r"(?:.*/)?wav48_silence_trimmed/([^/]+)/([^/]+)_(mic[12])\.flac$")


def input_files(root, config=None):
    return inputs(root, "*.zip", config)


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [safe_member(m.filename) for m in members]
            if len(set(names)) != len(names):
                raise ValueError("Duplicate VCTK ZIP member")
            lookup = dict(zip(names, members, strict=True))
            texts = {}
            speakers = {}
            for name, m in lookup.items():
                if "/txt/" in "/" + name and name.endswith(".txt"):
                    key = PurePosixPath(name).stem
                    if key in texts:
                        raise ValueError("Duplicate VCTK transcript ID")
                    texts[key] = (name, archive.read(m).decode("utf-8-sig").strip())
                elif PurePosixPath(name).name == "speaker-info.txt":
                    for line in archive.read(m).decode("utf8").splitlines()[1:]:
                        if line.strip():
                            key, *fields = line.split()
                            speakers[key] = fields
            audio_members = [(n, AUDIO.fullmatch(n)) for n in names if n.endswith(".flac")]
            matched = {m.group(2) for _, m in audio_members if m}
            unmatched_texts = len(set(texts) - matched)
            for name, match in audio_members:
                if match is None:
                    raise ValueError(f"Unexpected VCTK audio path: {name}")
                speaker, key, mic = match.groups()
                if not key.startswith(speaker + "_"):
                    raise ValueError("VCTK speaker/utterance mismatch")
                text_member, text = texts.get(key, (None, None))
                text = text or None
                yield make_record(
                    dataset_id="vctk",
                    source_snapshot=snapshot,
                    source_key=f"{key}_{mic}",
                    audio_bytes=archive.read(lookup[name]),
                    audio_name=PurePosixPath(name).name,
                    source_locator={
                        "root": "raw_tts",
                        "path": f"VCTK/{path.relative_to(root)}",
                        "member": name,
                        "metadata_member": text_member,
                    },
                    text=text,
                    text_kind="source_transcript" if text else None,
                    language="en",
                    speaker_id=f"vctk:{speaker}",
                    speaker_scope="dataset",
                    source_config="all",
                    recording_id=f"vctk:{key}",
                    group_id=f"vctk:{key}",
                    metadata={
                        "original_split": None,
                        "microphone": mic,
                        "upstream_utterance_id": key,
                        "upstream_speaker_info": speakers.get(speaker),
                        "missing_transcript": text is None,
                        "archive_transcripts_without_audio": unmatched_texts,
                    },
                )
