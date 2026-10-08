import io
import json
import tarfile
from pathlib import Path

import pytest

from tts_data_pipeline.convert import file_hash
from tts_data_pipeline.ingest.emilia2.metadata import scan_archive
from tts_data_pipeline.ingest.emilia2.planning import deduplicate


def make_source(tmp_path, name, kind="short", start=0, text="hello"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    short = dict(
        id="rec_001",
        text=text,
        language="en",
        speaker="rec_spk1",
        rel_start_samples=start,
        rel_end_samples=44100,
        duration=1.0,
    )
    meta = dict(
        id="rec_001" if kind == "short" else "carrier",
        type=kind,
        recording_id="rec",
        frames=44100,
        sample_rate=44100,
        short=[short],
    )
    with tarfile.open(path, "w") as tar:
        for member, raw in [("carrier.m4a", b"audio"), ("carrier.json", json.dumps(meta).encode())]:
            info = tarfile.TarInfo(member)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    with tarfile.open(path) as tar:
        Path(str(path) + ".idx").write_text(
            "".join(f"{m.name}\t{m.offset_data}\t{m.size}\n" for m in tar)
        )
    s = path.stat()
    return dict(
        path=name,
        bytes=s.st_size,
        mtime_ns=s.st_mtime_ns,
        index_sha256=file_hash(Path(str(path) + ".idx")),
    )


def prepare(tmp_path, specs):
    root = tmp_path / "raw"
    root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    sources = [make_source(root, f"data/{i}.tar", **s) for i, s in enumerate(specs)]
    (work / "plan.json").write_text(json.dumps(dict(root=str(root), sources=sources)))
    for i, s in enumerate(sources):
        scan_archive((root, work, i, s))
    return root, work, sources


def test_cross_archive_dedup_prefers_direct_short(tmp_path):
    _, work, _ = prepare(tmp_path, [dict(kind="dialogue"), dict(kind="short"), dict(kind="long")])
    r = deduplicate(work)
    assert (r["candidates"], r["selected"], r["redundant_candidates"]) == (3, 1, 2)
    assert [p["rows"] for p in r["partitions"]] == [0, 1, 0]
    assert deduplicate(work) == r


def test_bad_direct_candidate_does_not_hide_valid_long(tmp_path):
    _, work, _ = prepare(tmp_path, [dict(kind="short", start=-2), dict(kind="long")])
    r = deduplicate(work)
    assert r["selected"] == 1
    assert [p["rows"] for p in r["partitions"]] == [0, 1]


def test_conflicting_text_excluded_not_arbitrarily_selected(tmp_path):
    _, work, _ = prepare(tmp_path, [dict(kind="short"), dict(kind="dialogue", text="different")])
    r = deduplicate(work)
    assert r["selected"] == 0 and r["conflicting_ids"] == 1
    assert "identity_conflict" in (work / "candidate-exclusions.jsonl").read_text()


def test_metadata_restart_detects_changed_source(tmp_path):
    root, work, sources = prepare(tmp_path, [dict(kind="short")])
    (root / sources[0]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Source changed"):
        scan_archive((root, work, 0, sources[0]))


def test_negative_bounds_are_recorded_not_clamped(tmp_path):
    _, work, _ = prepare(tmp_path, [dict(kind="long", start=-100)])
    r = deduplicate(work)
    assert r["selected"] == 0 and r["invalid_ids"] == 1


def test_index_must_match_tar_headers(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    source = make_source(root, "data/a.tar")
    idx = Path(str(root / source["path"]) + ".idx")
    idx.write_text(idx.read_text().replace("512", "513", 1))
    source["index_sha256"] = file_hash(idx)
    with pytest.raises(ValueError, match="Tar/index"):
        scan_archive((root, tmp_path / "work", 0, source))


def test_crop_encoding_does_not_clip_and_roundtrips():
    import numpy as np
    import soundfile as sf

    from tts_data_pipeline.ingest.emilia2.publication import encoded_crop

    samples = np.array([[-1.2], [0.3], [1.1]], dtype=np.float32)
    raw, extension, error = encoded_crop(samples, 44100)
    assert extension == ".wav" and error == 0
    assert np.array_equal(sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)[0], samples)
    raw, extension, error = encoded_crop(samples / 2, 44100)
    assert extension == ".flac" and error <= 2**-24


def test_original_m4a_base_preserves_bytes_and_actual_length(tmp_path):
    import subprocess

    import numpy as np
    import soundfile as sf

    from tts_data_pipeline.audio_io import ffmpeg_path, read_audio
    from tts_data_pipeline.convert import decode_check
    from tts_data_pipeline.schema import make_record, validate_record

    try:
        binary = ffmpeg_path()
    except RuntimeError:
        pytest.skip("Pinned AAC decoder not installed")
    wav = tmp_path / "input.wav"
    m4a = tmp_path / "input.m4a"
    sf.write(wav, np.sin(np.arange(44100, dtype=np.float32) * 0.02) * 0.1, 44100)
    subprocess.run(
        [str(binary), "-v", "error", "-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(m4a)],
        check=True,
    )
    raw = m4a.read_bytes()
    audio, rate = read_audio(raw)
    row = make_record(
        dataset_id="test",
        source_snapshot="test",
        source_key="one",
        audio_bytes=raw,
        audio_name="one.m4a",
        source_locator={},
    )
    assert row["audio"]["bytes"] == raw
    assert row["num_frames"] == len(audio) and row["sample_rate"] == rate
    validate_record(row)
    decode_check(row)


def test_empty_winner_archive_has_no_fragments(tmp_path):
    from tts_data_pipeline.writer import write_batches

    assert list(write_batches(iter([]), tmp_path / "samples.lance")) == []


def test_unstable_partition_readback_never_creates_checkpoint(tmp_path, monkeypatch):
    from tts_data_pipeline.ingest.emilia2 import metadata

    root = tmp_path / "raw"
    root.mkdir()
    source = make_source(root, "data/a.tar")
    real_hash = metadata.file_hash

    def inconsistent_read(path):
        return "0" * 64 if Path(path).suffix == ".parquet" else real_hash(path)

    monkeypatch.setattr(metadata, "file_hash", inconsistent_read)
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="Partition readback mismatch"):
        scan_archive((root, work, 0, source))
    assert not (work / "metadata/00000.json").exists()
    # Retrying from the pinned source rebuilds the uncheckpointed partition.
    monkeypatch.setattr(metadata, "file_hash", real_hash)
    repaired = scan_archive((root, work, 0, source))
    assert repaired["sha256"] == real_hash(work / "metadata/00000.parquet")
