"""Publish deduplicated Emilia2 shorts using the standard base schema and Lance writer."""

import hashlib
import io
import json
import math
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import soundfile as sf

from ...audio_io import AudioDecodeError, aac_profile, decode_file
from ...contract import schema_description
from ...convert import decode_check, file_hash
from ...feature_runtime import write_json
from ...schema import VERSION, base_schema, digest, make_record, validate_record
from ...timestamps import BEIJING
from ...writer import (
    STORAGE_FORMAT,
    STORAGE_VERSION,
    commit_fragments,
    dependencies,
    file_batches,
    write_batches,
)
from .metadata import check_source

# Whole upstream categories left out of the release; their IDs never collide with kept ones.
EXCLUDED_CATEGORIES = {"ASMR"}


def excluded(source):
    return any(f"/{c}/" in source["path"] for c in EXCLUDED_CATEGORIES)


def ingestion_profile():
    return dict(
        name="emilia2-short-ingest-v2",
        decoder=aac_profile(),
        standalone="decoded; [0, container valid frames) drops AAC end padding",
        nested="integer native [start,end) from short annotation; no resampling",
        crop_encoding="FLAC PCM16 via round(gain*x*32768); gain<1 only when the peak exceeds PCM16",
        pcm16_max_quantization_error=2**-16,
        excluded_source_categories=sorted(EXCLUDED_CATEGORIES),
        dedup="valid standalone > valid long > valid dialogue; 128k before 64k within type",
        identity="source-transform-v1",
        implementation={p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        audio_io_sha256=file_hash(Path(__file__).parents[2] / "audio_io.py"),
        schema_sha256=file_hash(Path(__file__).parents[2] / "schema.py"),
        dependencies=dependencies(),
    )


def encoded_crop(audio, rate):
    """16-bit FLAC with explicit rounding; AAC overshoot past full scale is attenuated, not clipped.

    Returns the encoded bytes, the applied linear gain (1.0 unless attenuated) and the maximum
    absolute difference between the decoded result and gain * audio.
    """
    if not len(audio) or not np.isfinite(audio).all():
        raise AudioDecodeError("empty_or_nonfinite_crop")
    wave = audio.astype(np.float64)
    high, low = float(wave.max()), float(wave.min())
    gain = 1.0
    if round(high * 32768) > 32767 or round(low * 32768) < -32768:
        # PCM16 spans [-32768, 32767]; scale the side that overshoots the most.
        gain = min(32767 / (high * 32768) if high > 0 else 1.0, -1 / low if low < 0 else 1.0)
    scaled = np.round(wave * gain * 32768)
    buf = io.BytesIO()
    sf.write(buf, scaled.astype(np.int16), rate, format="FLAC", subtype="PCM_16")
    data = buf.getvalue()
    restored, _ = sf.read(io.BytesIO(data), dtype="float64", always_2d=True)
    expected = (wave[:, None] if wave.ndim == 1 else wave) * gain
    error = float(np.max(np.abs(restored - expected)))
    if restored.shape != expected.shape or error > 2**-16:
        raise ValueError("Crop encoding verification failed")
    return data, gain, error


def sample_record(candidate, source, source_sha, profile_id, blob, gain, quantization):
    # The source tar contains both the carrier bytes and exact integer short annotation.
    snapshot = "source-transform-v1:sha256:" + digest(
        dict(path=source["path"], sha256=source_sha, ingest_profile_id=profile_id)
    )
    rec = candidate["recording_id"]
    speaker = candidate["speaker"]
    language = candidate["language"]
    language = None if not language or language in {"unk", "unknown"} else language
    speaker = None if not speaker or speaker in {"unk", "unknown"} else speaker
    text = candidate["text"] if candidate["text"] and candidate["text"].strip() else None
    # No Lance parent object exists. Upstream carrier coordinates belong in the locator.
    return make_record(
        dataset_id="emilia2",
        source_snapshot=snapshot,
        source_key=candidate["short_id"],
        audio_bytes=blob,
        audio_name=candidate["short_id"] + ".flac",
        text=text,
        text_kind="source_transcript" if text else None,
        language=language,
        speaker_id=f"emilia2:{rec}:{speaker}" if speaker else None,
        speaker_scope="source_recording" if speaker else None,
        recording_id=f"emilia2:{rec}",
        group_id=f"emilia2:{rec}",
        source_config=source["path"].split("/")[0]
        + ("/ASMR" if "/ASMR/" in source["path"] else ""),
        source_locator=dict(
            root="raw_tts",
            path="Emilia2/" + source["path"],
            member=candidate["member"],
            metadata_member=candidate["metadata_member"],
            native_sample_rate=candidate["sample_rate"],
            source_start_frame=candidate["start"],
            source_end_frame=candidate["end"],
        ),
        metadata=dict(
            upstream=json.loads(candidate["metadata_json"]),
            carrier_recording_id=rec,
            source_audio_sha256=candidate["source_audio_sha256"],
            ingest_profile_id=profile_id,
            source_native_valid_frames=candidate["native_frames"],
            ingestion_operation="standalone_transcode"
            if candidate["carrier_type"] == "short"
            else "short_crop",
            crop_gain=gain,
            crop_quantization_max_abs=quantization,
            original_split=None,
            original_language=candidate["language"],
        ),
    )


def convert_archive(job):
    work, number = Path(job[0]), job[1]
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    plan = json.loads((work / "plan.json").read_text())
    publish = json.loads((work / "publication.json").read_text())
    if aac_profile() != publish["profile"]["decoder"]:
        raise ValueError("Pinned AAC decoder changed")
    source = plan["sources"][number]
    stage = Path(publish["stage"])
    checkpoint = work / "converted" / f"{number:05d}.json"
    partition = work / "winners" / f"{number:05d}.parquet"
    expected = json.loads((work / "dedup.json").read_text())["partitions"][number]
    metadata = work / "metadata" / f"{number:05d}.parquet"
    scanned = json.loads(metadata.with_suffix(".json").read_text())
    if file_hash(partition) != expected["sha256"] or file_hash(metadata) != scanned["sha256"]:
        raise ValueError("Input metadata or winner partition changed")
    if checkpoint.exists():
        result = json.loads(checkpoint.read_text())
        if result["publication_sha256"] != file_hash(work / "publication.json"):
            raise ValueError("Checkpoint publication changed")
        for shard in result["shards"]:
            if file_hash(stage / shard["path"]) != shard["sha256"]:
                raise ValueError("Checkpoint fragment changed")
        return result
    winner_rows = pq.read_table(partition)["row_number"].to_pylist()
    if excluded(source):
        result = dict(
            publication_sha256=file_hash(work / "publication.json"),
            source=source,
            excluded_category=True,
            rows=0,
            rejected=[],
            fragments=[],
            shards=[],
            excluded_rows=len(winner_rows),
            selected=len(winner_rows),
        )
        checkpoint.parent.mkdir(exist_ok=True)
        write_json(checkpoint, result)
        return result
    path = check_source(plan["root"], source)
    source_sha = file_hash(path)
    check_source(plan["root"], source)
    table = pq.read_table(metadata).take(pa.array(winner_rows, type=pa.int64()))
    selected = sorted(table.to_pylist(), key=lambda r: (r["offset"], r["start"]))
    groups = {}
    for row in selected:
        groups.setdefault(row["member"], []).append(row)
    counts = Counter()
    errors = []
    languages = Counter()
    formats = Counter()
    rates = Counter()
    audio_bytes = 0
    attenuated = 0
    duration = 0.0
    ordered = hashlib.sha256()

    def records():
        nonlocal audio_bytes, duration, attenuated
        with path.open("rb") as handle:
            for member, items in groups.items():
                first = items[0]
                handle.seek(first["offset"])
                raw = handle.read(first["bytes"])
                if len(raw) != first["bytes"]:
                    raise ValueError("Truncated carrier audio")
                sha = hashlib.sha256(raw).hexdigest()
                try:
                    with tempfile.TemporaryDirectory(prefix="emilia2-crop-") as tmp:
                        # Standalone shorts are carriers whose annotation is [0, valid frames).
                        inp = Path(tmp) / ("input" + Path(member).suffix)
                        inp.write_bytes(raw)
                        wav = Path(tmp) / "decoded.wav"
                        decode_file(inp, wav)
                        wave = sf.SoundFile(wav)
                        try:
                            for item in items:
                                item["source_audio_sha256"] = sha
                                try:
                                    if wave.samplerate != item["sample_rate"]:
                                        raise ValueError("Source native rate disagrees with decode")
                                    if item["end"] > len(wave):
                                        raise AudioDecodeError("short_end_beyond_actual_decode")
                                    wave.seek(item["start"])
                                    crop = wave.read(
                                        item["end"] - item["start"],
                                        dtype="float32",
                                        always_2d=True,
                                    )
                                    blob, gain, quant = encoded_crop(crop, wave.samplerate)
                                    row = sample_record(
                                        item,
                                        source,
                                        source_sha,
                                        publish["profile_id"],
                                        blob,
                                        gain,
                                        quant,
                                    )
                                    if row["sample_rate"] != item["sample_rate"]:
                                        raise ValueError(
                                            "Standalone source rate disagrees with decode"
                                        )
                                    validate_record(row)
                                    decode_check(row)
                                except (AudioDecodeError, sf.LibsndfileError) as exc:
                                    errors.append(
                                        dict(
                                            short_id=item["short_id"],
                                            member=member,
                                            reason=str(exc),
                                        )
                                    )
                                    continue
                                counts["rows"] += 1
                                languages[row["language"]] += 1
                                formats[".flac"] += 1
                                attenuated += gain < 1
                                rates[str(row["sample_rate"])] += 1
                                audio_bytes += len(blob)
                                duration += row["duration_seconds"]
                                ordered.update((row["sample_id"] + row["record_revision"]).encode())
                                yield row
                        finally:
                            wave.close()
                except (AudioDecodeError, sf.LibsndfileError) as exc:
                    errors.extend(
                        dict(short_id=item["short_id"], member=member, reason=str(exc))
                        for item in items
                    )
                if len(errors) >= max(16, math.ceil(len(selected) / 4)):
                    raise RuntimeError("Widespread audio failures; stop instead of mass exclusion")

    def batches():
        buf = []
        size = 0
        for row in records():
            buf.append(row)
            size += len(row["audio"]["bytes"])
            if size >= 16 * 1024**2:
                yield pa.RecordBatch.from_pylist(buf, schema=base_schema())
                buf = []
                size = 0
        if buf:
            yield pa.RecordBatch.from_pylist(buf, schema=base_schema())

    # Empty winner partitions are valid: every candidate may belong to another carrier.
    fragments = [f.to_json() for f in write_batches(batches(), stage / "samples.lance")]
    shards = []
    restored = hashlib.sha256()
    n = 0
    for fragment in fragments:
        if len(fragment["files"]) != 1:
            raise ValueError("Expected self-contained base fragment")
        p = stage / "samples.lance/data" / fragment["files"][0]["path"]
        shards.append(
            dict(
                path=str(p.relative_to(stage)),
                sha256=file_hash(p),
                rows=fragment["physical_rows"],
                bytes=p.stat().st_size,
            )
        )
        for batch in file_batches(p):
            for row in batch.to_pylist():
                validate_record(row)
                decode_check(row)
                restored.update((row["sample_id"] + row["record_revision"]).encode())
                n += 1
    if (
        n != counts["rows"]
        or restored.digest() != ordered.digest()
        or n + len(errors) != len(selected)
    ):
        raise ValueError("Write/readback/count conservation failed")
    check_source(plan["root"], source)
    result = dict(
        publication_sha256=file_hash(work / "publication.json"),
        source=source,
        source_sha256=source_sha,
        rows=n,
        rejected=errors,
        fragments=fragments,
        shards=shards,
        ordered_record_digest=ordered.hexdigest(),
        duration_seconds=duration,
        audio_bytes=audio_bytes,
        languages=dict(languages),
        sample_rates=dict(rates),
        formats=dict(formats),
        attenuated_rows=attenuated,
        selected=len(selected),
    )
    checkpoint.parent.mkdir(exist_ok=True)
    write_json(checkpoint, result)
    return result


def finalize(work):
    """Global source-key + sample-ID audit, then commit and atomically publish."""
    import sqlite3

    work = Path(work)
    plan = json.loads((work / "plan.json").read_text())
    pub = json.loads((work / "publication.json").read_text())
    stage = Path(pub["stage"])
    dedup = json.loads((work / "dedup.json").read_text())
    fragments = []
    shards = []
    inputs = []
    rows = failed = excluded_rows = audio_bytes = 0
    duration = 0.0
    languages = Counter()
    rates = Counter()
    formats = Counter()
    speakers = Counter()
    excluded_files, excluded_records = [], []
    with tempfile.TemporaryDirectory(prefix="emilia2-publish-") as tmp:
        db = sqlite3.connect(str(Path(tmp) / "ids.sqlite"))
        try:
            db.execute("CREATE TABLE ids(sample_id TEXT PRIMARY KEY,source_key TEXT UNIQUE)")
            for i, s in enumerate(plan["sources"]):
                check_source(plan["root"], s)
                cp = work / "converted" / f"{i:05d}.json"
                r = json.loads(cp.read_text())
                if r["publication_sha256"] != file_hash(work / "publication.json"):
                    raise ValueError("Checkpoint publication changed")
                if r.get("excluded_category"):
                    if not excluded(s):
                        raise ValueError("Unexpected category exclusion")
                    excluded_rows += r["excluded_rows"]
                    excluded_files.append(
                        dict(
                            path=s["path"],
                            bytes=s["bytes"],
                            index_sha256=s["index_sha256"],
                            rows=r["excluded_rows"],
                            reason="excluded_category",
                        )
                    )
                    continue
                rows += r["rows"]
                failed += len(r["rejected"])
                excluded_records.extend(
                    dict(source_path=s["path"], stage="decode", **e) for e in r["rejected"]
                )
                audio_bytes += r["audio_bytes"]
                duration += r["duration_seconds"]
                languages.update(r["languages"])
                rates.update(r["sample_rates"])
                formats.update(r["formats"])
                inputs.append(dict(path=s["path"], bytes=s["bytes"], sha256=r["source_sha256"]))
                fragments.extend(r["fragments"])
                shards.extend(r["shards"])
                for shard in r["shards"]:
                    if file_hash(stage / shard["path"]) != shard["sha256"]:
                        raise ValueError("Fragment changed before publication")
                    for batch in file_batches(
                        stage / shard["path"], columns=["sample_id", "source_key", "speaker_id"]
                    ):
                        speakers.update(x for x in batch["speaker_id"].to_pylist() if x)
                        db.executemany(
                            "INSERT INTO ids VALUES (?,?)",
                            zip(
                                batch["sample_id"].to_pylist(),
                                batch["source_key"].to_pylist(),
                                strict=True,
                            ),
                        )
                db.commit()
            if not rows or rows + failed + excluded_rows != dedup["selected"]:
                raise ValueError("Final output count mismatch or empty dataset")
            if db.execute("SELECT COUNT(*) FROM ids").fetchone()[0] != rows:
                raise ValueError("Global unique identity mismatch")
        finally:
            db.close()
    with (work / "candidate-exclusions.jsonl").open() as handle:
        excluded_records.extend(dict(stage="dedup", **json.loads(line)) for line in handle)
    rejected_rows = failed + dedup["invalid_ids"] + dedup["conflicting_ids"]
    if len(excluded_records) != rejected_rows:
        raise ValueError("Excluded record evidence does not match rejected rows")
    ds = commit_fragments(stage / "samples.lance", fragments)
    if ds.count_rows() != rows:
        raise ValueError("Lance committed row count mismatch")
    ds.create_scalar_index("source_key", "BTREE")
    manifest = dict(
        layout="lance-v1",
        artifact_kind="base",
        status="complete",
        dataset="emilia2",
        dataset_id="emilia2",
        release_id=pub["release_id"],
        schema_version=VERSION,
        contract_version=VERSION,
        schema_sha256=digest(schema_description(base_schema())),
        identity_scheme="source-transform-v1",
        source_snapshot_scope="source tar + ingest profile",
        ingest_profile=pub["profile"],
        ingest_profile_id=pub["profile_id"],
        table_path="samples.lance",
        lance_version=ds.version,
        rows=rows,
        inputs=inputs,
        input_manifest_sha256=digest(inputs),
        source_selection="all listed standalone shorts and "
        "nested shorts; globally deduplicated upstream short ID, "
        "explicit invalid/conflict exclusions; excluded categories: "
        + ", ".join(sorted(EXCLUDED_CATEGORIES)),
        excluded_category_rows=excluded_rows,
        storage_format=STORAGE_FORMAT,
        storage_version=STORAGE_VERSION,
        dependencies=dependencies(),
        # Every checkpoint is bound to publication.json, which fixes this implementation.
        code_sha256=pub["profile"]["implementation"],
        checkpoint_code_versions={
            digest(pub["profile"]["implementation"]): pub["profile"]["implementation"]
        },
        code_migration=None,
        finalization=None,  # Bulk identity-audit metrics do not apply to this adapter.
        audio_bytes=audio_bytes,
        duration_seconds=duration,
        output_bytes=sum(s["bytes"] for s in shards),
        shards=shards,
        languages=dict(languages),
        sample_rates=dict(rates),
        audio_formats=dict(formats),
        source_splits={"train": rows},
        speakers=dict(speakers),
        excluded_source_files=excluded_files,
        excluded_source_records=excluded_records,
        rejected_rows=rejected_rows,
        validation=dict(
            unique_ids=rows,
            unique_source_keys=rows,
            lance_rows_verified=rows,
            fully_decoded_audio=rows,
        ),
        dedup={k: v for k, v in dedup.items() if k != "partitions"},
        finished_at=datetime.now(BEIJING).isoformat(),
    )
    # Candidate/checkpoint evidence stays in the work directory, pinned by the dedup hashes;
    # the release keeps the common layout of manifest.json + samples.lance.
    write_json(stage / "manifest.json", manifest)
    stage.rename(pub["output"])
    return manifest
