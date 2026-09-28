"""WenetSpeech4TTS: disjoint physical archives, Basic union, embedded transcripts."""

import math
import re
import tarfile
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import pairs, safe_member

IDENTITY_SCHEME = "source-unit-v1"
TIERS = ("Basic", "Standard", "Premium")


def input_files(root, config=None):
    if config in (None, "all", "Basic"):
        tiers = TIERS
    elif config in TIERS:
        tiers = TIERS[TIERS.index(config) :]
    else:
        raise ValueError("Supported config: all/Basic/Standard/Premium")
    files = sorted(p for tier in tiers for p in (root / tier).glob("*.tar.gz"))
    if not files:
        raise ValueError("No WenetSpeech4TTS archives")
    return files


def dependency_files(root, path):
    return [root / "filelists/Basic_filelist.lst", root / "DNSMOS_P808Scores/Basic_DNSMOS.lst"]


def archive_metadata(root, path):
    prefix = path.name.removesuffix(".tar.gz")
    tier = path.parent.name
    if tier not in TIERS or not prefix.startswith(f"WenetSpeech4TTS_{tier}_"):
        raise ValueError(f"Unexpected Wenet archive path: {path}")
    filelist, scores = dependency_files(root, path)
    rows = {}
    with filelist.open() as handle:
        for line in handle:
            key, audio, text = line.rstrip("\r\n").split("\t")
            if f"/{prefix}/" not in audio:
                continue
            expected_audio = f"../{tier}/{prefix}/wavs/{key}.wav"
            expected_text = f"../{tier}/{prefix}/txts/{key}.txt"
            if (audio, text) != (expected_audio, expected_text) or key in rows:
                raise ValueError(f"Invalid or duplicate Wenet filelist entry: {key}")
            rows[key] = {"audio": f"{prefix}/wavs/{key}.wav", "text": f"{prefix}/txts/{key}.txt"}
    with scores.open() as handle:
        for line in handle:
            key, score = line.split()
            if key in rows:
                value = float(score)
                if "dnsmos" in rows[key] or not math.isfinite(value) or not 1 <= value <= 5:
                    raise ValueError(f"Invalid or duplicate DNSMOS score: {key}")
                rows[key]["dnsmos"] = value
    if not rows or any("dnsmos" not in r for r in rows.values()):
        raise ValueError("Missing Wenet filelist entries or source DNSMOS scores")
    return tier, rows


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        tier, metadata = archive_metadata(root, path)
        with pairs() as store, tarfile.open(path, "r|*") as archive:
            emitted = set()
            for member in archive:
                if not member.isfile():
                    continue
                name = safe_member(member.name)
                if not name.endswith((".wav", ".txt")):
                    raise ValueError(f"Unexpected Wenet member: {name}")
                key = PurePosixPath(name).stem
                if key not in metadata:
                    raise ValueError(f"Wenet member missing from filelist: {name}")
                meta = metadata[key]
                if name == meta["audio"]:
                    result = store.put(key, data=archive.extractfile(member).read())
                elif name == meta["text"]:
                    raw = archive.extractfile(member).read().decode("utf-8-sig")
                    first, *rest = raw.splitlines()
                    declared, text = first.split("\t", 1)
                    if declared != key:
                        raise ValueError("Wenet transcript ID mismatch")
                    result = store.put(
                        key, metadata={"text": text, "raw": raw, "extra_lines": rest}
                    )
                else:
                    raise ValueError(f"Wenet member path mismatch: {name}")
                if result is None:
                    continue
                data, transcript = result
                emitted.add(key)
                recording = re.split(r"_S\d", key, maxsplit=1)[0]
                text = transcript["text"] or None
                yield make_record(
                    dataset_id="wenetspeech4tts",
                    source_snapshot=snapshot,
                    source_key=key,
                    source_locator={
                        "root": "raw_tts",
                        "path": f"WenetSpeech4TTS/{path.relative_to(root)}",
                        "member": meta["audio"],
                        "metadata_member": meta["text"],
                    },
                    audio_bytes=data,
                    audio_name=f"{key}.wav",
                    text=text,
                    text_kind="source_transcript" if text else None,
                    language="zh",
                    source_config=tier,
                    recording_id=f"wenetspeech4tts:{recording}",
                    group_id=f"wenetspeech4tts:{recording}",
                    metadata={
                        "original_split": None,
                        "physical_partition": tier,
                        "logical_subsets": list(TIERS[: TIERS.index(tier) + 1]),
                        "upstream_dnsmos_p808": meta["dnsmos"],
                        "upstream_transcript": transcript["raw"],
                    },
                )
            store.finish()
            if emitted != metadata.keys():
                raise ValueError("Wenet archive does not cover declared filelist")
