"""MLS SIDON: stream FLAC/metadata pairs without extracting archives to disk."""

import json
import tarfile
from pathlib import Path

from ..schema import digest, make_record

LANGUAGES = {
    "english": "en",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "polish": "pl",
    "dutch": "nl",
    "portuguese": "pt",
}
SPLITS = {"train": "train", "dev": "valid", "test": "test"}


def input_files(root: Path, config: str | None = None) -> list[Path]:
    if config is not None and config not in LANGUAGES:
        raise ValueError(f"Unknown MLS SIDON language: {config}")
    return sorted((root / config if config else root).rglob("*.tar.gz"))


def archive_pairs(path: Path):
    pending, seen = {}, set()
    pending_bytes = 0
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            if member.name.endswith(".metadata.json"):
                key, kind = member.name[: -len(".metadata.json")], "metadata"
            elif member.name.endswith(".flac"):
                key, kind = member.name[: -len(".flac")], "audio"
            else:
                raise ValueError(f"Unexpected MLS member: {member.name}")
            pair = pending.setdefault(key, {})
            if key in seen or kind in pair:
                raise ValueError(f"Duplicate MLS member: {member.name}")
            if member.size > 64 * 1024 * 1024 or pending_bytes + member.size > 64 * 1024 * 1024:
                raise ValueError("MLS unmatched pair buffer exceeds 64 MiB")
            data = archive.extractfile(member).read()
            pending_bytes += len(data)
            pair[kind] = (member.name, data)
            if len(pair) == 2:
                metadata = json.loads(pair["metadata"][1])
                if str(metadata["id"]) != Path(key).name:
                    raise ValueError(f"MLS metadata ID does not match audio member: {key}")
                del pending[key]
                seen.add(key)
                pending_bytes -= sum(len(item[1]) for item in pair.values())
                yield pair["audio"][0], pair["audio"][1], pair["metadata"][0], metadata
        if pending:
            raise ValueError(f"Unpaired MLS members in {path.name}: {list(pending)[:5]}")


def iter_records(root: Path, snapshot: str, *, files: list[Path] | None = None):
    for path in files if files is not None else input_files(root):
        relative = path.relative_to(root)
        language = relative.parts[0]
        original_archive_split = path.name.split("-")[0]
        split = SPLITS[original_archive_split]
        for member, audio, metadata_member, metadata in archive_pairs(path):
            speaker = metadata.get("speaker_id")
            text = metadata.get("transcript") or None
            original = metadata.get("original_path")
            recording = f"mls_sidon:{language}:recording:{digest(original)}" if original else None
            yield make_record(
                dataset_id="mls_sidon",
                source_snapshot=snapshot,
                source_key=f"{language}:{metadata['id']}",
                source_locator={
                    "root": "raw_tts",
                    "path": f"mls_sidon/{relative}",
                    "member": member,
                    "metadata_member": metadata_member,
                },
                audio_bytes=audio,
                audio_name=Path(member).name,
                text=text,
                text_kind="source_transcript" if text else None,
                language=LANGUAGES[language],
                speaker_id=f"mls_sidon:{language}:{speaker}" if speaker is not None else None,
                speaker_scope="dataset_language" if speaker is not None else None,
                source_config=language,
                source_split="train",
                recording_id=recording,
                group_id=recording,
                metadata={
                    "upstream": metadata,
                    "original_split": split,
                    "original_split_group": split,
                    "original_archive_split": original_archive_split,
                },
            )
