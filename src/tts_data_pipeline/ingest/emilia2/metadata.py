"""Pinned source inventory and restartable, CPU-only short candidate discovery."""

import hashlib
import json
import tarfile
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ...convert import file_hash
from ...feature_runtime import write_json
from ...schema import digest

COLUMNS = {
    "short_id": pa.string(),
    "recording_id": pa.string(),
    "text": pa.string(),
    "language": pa.string(),
    "speaker": pa.string(),
    "carrier_type": pa.string(),
    "member": pa.string(),
    "metadata_member": pa.string(),
    "offset": pa.int64(),
    "bytes": pa.int64(),
    "native_frames": pa.int64(),
    "sample_rate": pa.int32(),
    "start": pa.int64(),
    "end": pa.int64(),
    "priority": pa.int16(),
    "error": pa.string(),
    "semantic_hash": pa.string(),
    "metadata_json": pa.string(),
}
CANDIDATE_SCHEMA = pa.schema(list(COLUMNS.items()))
SOURCE_DIRS = ("Emilia2_TTS_m4a/data", "Emilia2_TTS_m4a/ASMR", "Emilia2_TTS_m4a_64kbps/data")


def inventory(root):
    root = Path(root).resolve()
    paths = sorted(p for directory in SOURCE_DIRS for p in (root / directory).glob("*.tar"))
    if not paths:
        raise ValueError("No Emilia2 archives")
    result = []
    for p in paths:
        idx = Path(str(p) + ".idx")
        if not idx.is_file():
            raise ValueError(f"Missing index: {idx}")
        stat = p.stat()
        result.append(
            dict(
                path=p.relative_to(root).as_posix(),
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                index_sha256=file_hash(idx),
            )
        )
    return result


def check_source(root, source):
    p = Path(root) / source["path"]
    s = p.stat()
    if (s.st_size, s.st_mtime_ns) != (source["bytes"], source["mtime_ns"]):
        raise ValueError(f"Source changed: {source['path']}")
    if file_hash(Path(str(p) + ".idx")) != source["index_sha256"]:
        raise ValueError(f"Index changed: {p}")
    return p


def indexed_members(handle, path):
    """Verify the index against every tar header, including pairing and final padding."""
    entries = {}
    for line in Path(str(path) + ".idx").read_text().splitlines():
        name, offset, size = line.split("\t")
        if name in entries or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError(f"Duplicate or unsafe tar member: {name}")
        entries[name] = (int(offset), int(size))
    actual = {}
    # Iteration seeks past audio payloads, without decoding or retaining TarInfo objects.
    with tarfile.open(fileobj=handle, mode="r:") as archive:
        for member in archive:
            if not member.isfile() or member.name in actual:
                raise ValueError(f"Unexpected tar member: {member.name}")
            actual[member.name] = (member.offset_data, member.size)
            archive.members.clear()
        handle.seek(archive.offset)
        padding = handle.read()
        if len(padding) < 1024 or len(padding) % 512 or padding.strip(b"\0"):
            raise ValueError(f"Invalid tar terminator: {path}")
    if actual != entries:
        raise ValueError(f"Tar/index members disagree: {path}")
    stems = Counter()
    for name in entries:
        suffix = Path(name).suffix
        if suffix not in {".json", ".m4a", ".flac"}:
            raise ValueError(f"Unexpected member suffix: {name}")
        stems[name.rsplit(".", 1)[0]] += 1
    if any(n != 2 for n in stems.values()):
        raise ValueError(f"Unpaired source members: {path}")
    return entries


def candidates(meta, audio_member, metadata_member, location, variant):
    kind = meta["type"]
    if kind not in {"short", "long", "dialogue"}:
        raise ValueError(f"Unknown carrier type: {kind}")
    frames, rate = meta["frames"], meta["sample_rate"]
    if type(frames) is not int or frames <= 0 or type(rate) is not int or rate <= 0:
        raise ValueError("Invalid native timeline")
    shorts = meta.get("short") or []
    if not isinstance(shorts, list):
        raise ValueError("short must be a list")
    # A standalone short must describe itself, never an unrelated subsegment.
    if kind == "short" and (len(shorts) != 1 or shorts[0].get("id") != meta.get("id")):
        raise ValueError("Standalone short identity/list mismatch")
    for short in shorts:
        sid, rec = short.get("id"), meta.get("recording_id")
        if not isinstance(sid, str) or not sid or not isinstance(rec, str) or not rec:
            raise ValueError("Missing short/recording identity")
        a, b = short.get("rel_start_samples"), short.get("rel_end_samples")
        error = None
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= frames:
            error = "invalid_short_bounds"
        elif kind == "short" and (a != 0 or b != frames):
            error = "standalone_short_not_full_carrier"
        text, language, speaker = (short.get(k) for k in ("text", "language", "speaker"))
        if any(v is not None and not isinstance(v, str) for v in (text, language, speaker)):
            raise ValueError("Invalid short text/language/speaker types")
        semantic = digest([rec, text.strip() if text else None, language, speaker])
        yield dict(
            short_id=sid,
            recording_id=rec,
            text=text,
            language=language,
            speaker=speaker,
            carrier_type=kind,
            member=audio_member,
            metadata_member=metadata_member,
            offset=location[0],
            bytes=location[1],
            native_frames=frames,
            sample_rate=rate,
            start=a if type(a) is int else None,
            end=b if type(b) is int else None,
            priority={"short": 0, "long": 2, "dialogue": 4}[kind] + variant,
            error=error,
            semantic_hash=semantic,
            metadata_json=json.dumps(
                dict(
                    short=short,
                    carrier_id=meta["id"],
                    carrier_type=kind,
                    tags=meta.get("tags"),
                    languages=meta.get("languages"),
                    declared_duration=meta.get("duration"),
                ),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )


def scan_archive(job):
    root, work, number, source = job
    work = Path(work)
    checkpoint = work / "metadata" / f"{number:05d}.json"
    output = checkpoint.with_suffix(".parquet")
    path = check_source(root, source)
    if checkpoint.exists():
        previous = json.loads(checkpoint.read_text())
        if previous["source"] != source or file_hash(output) != previous["sha256"]:
            raise ValueError("Metadata checkpoint mismatch")
        return previous
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    types = Counter()
    buffer = []
    payload_hash = hashlib.sha256()
    temp = output.with_suffix(".parquet.tmp")
    with (
        path.open("rb") as handle,
        pq.ParquetWriter(temp, CANDIDATE_SCHEMA, compression="zstd") as writer,
    ):
        entries = indexed_members(handle, path)
        for name, (offset, size) in entries.items():
            if not name.endswith(".json"):
                continue
            handle.seek(offset)
            raw = handle.read(size)
            if len(raw) != size:
                raise ValueError("Truncated metadata")
            payload_hash.update(raw)
            meta = json.loads(raw)
            audio_names = [
                name[:-5] + ext for ext in (".m4a", ".flac") if name[:-5] + ext in entries
            ]
            if len(audio_names) != 1:
                raise ValueError("Ambiguous audio/JSON pairing")
            audio = audio_names[0]
            counts["carriers"] += 1
            emitted = list(
                candidates(meta, audio, name, entries[audio], int("_64kbps/" in source["path"]))
            )
            counts["carriers_without_short"] += not emitted
            for row in emitted:
                counts["candidates"] += 1
                counts[row["error"] or "valid_candidates"] += 1
                types[row["carrier_type"]] += 1
            buffer.extend(emitted)
            if len(buffer) >= 4096:
                writer.write_table(pa.Table.from_pylist(buffer, schema=CANDIDATE_SCHEMA))
                buffer.clear()
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=CANDIDATE_SCHEMA))
    check_source(root, source)
    temp.replace(output)
    report = dict(
        source=source,
        sha256=file_hash(output),
        counts=dict(counts),
        types=dict(types),
        json_payload_sha256=payload_hash.hexdigest(),
    )
    write_json(checkpoint, report)
    return report
