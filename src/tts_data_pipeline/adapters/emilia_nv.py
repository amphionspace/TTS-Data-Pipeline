"""Emilia-NV (NVSpeech): Mandarin ASR text with inline paralinguistic tags, no speaker."""

import json
import re
import tarfile
from pathlib import Path

from ..schema import make_record
from ._archives import check_tar_end, inputs, pairs, safe_member

# The 18 upstream categories; text and category must name the same set.
TAGS = frozenset(
    "[" + name + "]"
    for name in (
        "Breathing Laughter Cough Sigh Confirmation-en Question-en Question-ah Question-oh "
        "Surprise-ah Surprise-oh Dissatisfaction-hnn Uhm Shh Crying Surprise-wa Surprise-yo "
        "Question-ei Question-yi"
    ).split()
)
TAG = re.compile(r"\[[^\[\]]*\]")


def input_files(root, config=None):
    files = inputs(root, "webdataset-*.tar", config)
    if any(not re.fullmatch(r"webdataset-\d{6}\.tar", p.name) for p in files):
        raise ValueError("Unexpected Emilia-NV archive name")
    return files


def checked_text(key, text, meta):
    if not isinstance(meta, dict) or set(meta) != {"category"}:
        raise ValueError(f"Emilia-NV metadata must contain only category: {key}")
    category = meta["category"]
    if not isinstance(category, str):
        raise ValueError(f"Emilia-NV category must be a string: {key}")
    categories = sorted(set(filter(None, category.split(","))))
    found = TAG.findall(text)
    if not set(found) <= TAGS or set(categories) != set(found):
        raise ValueError(f"Emilia-NV inline tags do not match category: {key}")
    return categories


def archive_pairs(path):
    """Text and category are tiny; audio waits in the disk-backed pair store."""
    prefix = path.stem.removeprefix("webdataset-")
    pending = {}
    with pairs() as store, path.open("rb") as handle:
        with tarfile.open(fileobj=handle, mode="r:") as archive:
            for member in archive:
                name = safe_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError(f"Unsupported Emilia-NV member type: {name}")
                key, suffix = name.rsplit(".", 1) if "." in name else (name, "")
                if suffix not in {"wav", "txt", "json"} or not re.fullmatch(
                    rf"{prefix}_\d{{6}}", key
                ):
                    raise ValueError(f"Unexpected Emilia-NV member: {name}")
                data = archive.extractfile(member).read()
                if len(data) != member.size:
                    raise ValueError(f"Truncated Emilia-NV member: {name}")
                archive.members.clear()
                if suffix == "wav":
                    result = store.put(key, data=data)
                else:
                    parts = pending.setdefault(key, {})
                    if suffix in parts:
                        raise ValueError(f"Duplicate archive pairing key: {key}")
                    parts[suffix] = data.decode("utf-8") if suffix == "txt" else json.loads(data)
                    if len(parts) < 2:
                        continue
                    del pending[key]
                    result = store.put(key, metadata={"text": parts["txt"], "meta": parts["json"]})
                if result is not None:
                    yield key, *result
            check_tar_end(handle, archive, path, "Emilia-NV")
        if pending:
            raise ValueError(f"Unpaired Emilia-NV text/category members: {sorted(pending)[:5]}")
        store.finish()


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        relative = path.relative_to(root)
        for key, audio, parts in archive_pairs(path):
            text, meta = parts["text"], parts["meta"]
            categories = checked_text(key, text, meta)
            text = text if text.strip() else None
            yield make_record(
                dataset_id="emilia_nv",
                source_snapshot=snapshot,
                source_key=key,
                source_locator={
                    "root": "raw_tts",
                    "path": f"Emilia-NV/{relative}",
                    "member": key + ".wav",
                    "text_member": key + ".txt",
                    "metadata_member": key + ".json",
                },
                # Members are named .wav but most bytes are MP3; keep upstream name and bytes.
                audio_bytes=audio,
                audio_name=Path(key + ".wav").name,
                text=text,
                text_kind="source_asr_paralinguistic" if text else None,
                language="zh",
                source_config="all",
                metadata={
                    "upstream": meta,
                    "paralinguistic_tags": categories,
                    "original_config": None,
                    "original_split": None,
                },
            )
