"""Shared Emilia JSON/MP3 pairing; dataset identity mappings live in each adapter."""

import json
import tarfile
from pathlib import Path

from ._archives import pairs, safe_member

LANGUAGES = {code: code.lower() for code in ("DE", "EN", "FR", "JA", "KO", "ZH")}


def input_files(root, config=None):
    if config not in (None, "all", *LANGUAGES):
        raise ValueError(f"Unknown Emilia language config: {config}")
    files = sorted(root.glob("*/*.tar"))
    if any(p.parent.name not in LANGUAGES for p in files):
        raise ValueError("Unknown Emilia source language directory")
    if config not in (None, "all"):
        files = [p for p in files if p.parent.name == config]
    if not files:
        raise ValueError("No Emilia tar archives selected")
    return files


def archive_pairs(path):
    """Pair by full member stem with bounded memory, including nonadjacent records."""
    with pairs() as store, path.open("rb") as handle:
        with tarfile.open(fileobj=handle, mode="r:") as archive:
            for member in archive:
                name = safe_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"Unsupported Emilia member type: {name}")
                suffix = Path(name).suffix
                if suffix not in {".mp3", ".json"}:
                    raise ValueError(f"Unexpected Emilia member: {name}")
                key = name[: -len(suffix)]
                data = archive.extractfile(member).read()
                if len(data) != member.size:
                    raise ValueError(f"Truncated Emilia member: {name}")
                if suffix == ".json":
                    meta = json.loads(data)
                    if not isinstance(meta, dict):
                        raise ValueError(f"Emilia metadata must be an object: {name}")
                    result = store.put(key, metadata=meta)
                else:
                    result = store.put(key, data=data)
                if result is not None:
                    audio, metadata = result
                    yield key + ".mp3", key + ".json", audio, metadata
                # Do not accumulate TarInfo objects for the entire archive.
                archive.members.clear()
            # Plain tar must end with at least two zero blocks. Check all padding,
            # including content after an early zero header; tarfile alone stops there.
            handle.seek(archive.offset)
            trailer_bytes = 0
            while block := handle.read(1024 * 1024):
                trailer_bytes += len(block)
                if block.strip(b"\0"):
                    raise ValueError(f"Nonzero content after Emilia tar terminator: {path}")
            if trailer_bytes < 1024 or trailer_bytes % 512:
                raise ValueError(f"Missing or truncated Emilia tar terminator: {path}")
        store.finish()


def checked_metadata(meta, id_field, member, language):
    key = meta.get(id_field)
    if not isinstance(key, str) or key != Path(member).stem:
        raise ValueError(f"Emilia metadata ID does not match member: {member}")
    if meta.get("language") != LANGUAGES[language]:
        raise ValueError(f"Emilia metadata language does not match directory: {member}")
    text, speaker = meta.get("text"), meta.get("speaker")
    for value in (text, speaker):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Emilia text and speaker must be strings or null: {member}")
    return (
        key,
        text if text and text.strip() else None,
        speaker if speaker and speaker.strip() else None,
    )
