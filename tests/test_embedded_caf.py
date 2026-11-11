import csv
import hashlib
import importlib.util
import io
import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tts_data_pipeline import selection
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_embedded_caf.py"
spec = importlib.util.spec_from_file_location("audit_embedded_caf", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
embedded_caf, wav_data_offset = audit.embedded_caf, audit.wav_data_offset


def caf_wav(pcm):
    """WAV whose data chunk is a CAF file (the KeSpeech defect), little-endian lpcm."""
    desc = struct.pack(">dIIIIIII", 16000.0, 0x6C70636D, 2, 2, 1, 1, 1, 16)
    caf = b"caff" + struct.pack(">HH", 1, 0)
    caf += b"desc" + struct.pack(">q", len(desc)) + desc
    caf += b"free" + struct.pack(">q", 4) + b"\0" * 4
    caf += b"data" + struct.pack(">q", -1) + struct.pack(">I", 0) + pcm
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1, np.int16), 16000, subtype="PCM_16", format="WAV")
    head = buf.getvalue()[:36]
    return head[:4] + struct.pack("<I", 36 + len(caf)) + head[8:36] + b"data" + struct.pack("<I", len(caf)) + caf


def test_detects_embedded_caf_and_its_pcm_offset():
    pcm = np.arange(8, dtype="<i2").tobytes()
    raw = caf_wav(pcm)
    start, offset = embedded_caf(raw)
    assert start == wav_data_offset(raw) == 44
    assert raw[offset:] == pcm


def test_plain_wav_is_not_flagged():
    buf = io.BytesIO()
    sf.write(buf, np.full(160, 0.1, np.float32), 16000, subtype="PCM_16", format="WAV")
    assert embedded_caf(buf.getvalue()) is None


def test_audio_content_evidence_yields_one_audio_scoped_103_per_audio(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "reports").mkdir(parents=True)
    tsv = repo / "reports" / "findings.tsv"
    with tsv.open("w", newline="") as stream:
        w = csv.writer(stream, delimiter="\t", lineterminator="\n")
        w.writerow(["dataset", "sample_id", "audio_sha256", "header_bytes"])
        w.writerow(["flexivoice", "a", "h1", "4096"])
        w.writerow(["flexivoice", "b", "h1", "4096"])  # an instruction variant of the same audio
        w.writerow(["flexivoice", "c", "h2", "4096"])
    sha = hashlib.sha256(tsv.read_bytes()).hexdigest()
    monkeypatch.setattr(selection, "__file__", str(repo / "src" / "pkg" / "selection.py"))
    rules = {"audio_content_evidence": [dict(
        dataset_id="flexivoice", path="reports/findings.tsv", sha256=sha, expected_audio=2,
        finding="embedded_caf_header", record_fields=["sample_id", "header_bytes"])]}
    inputs = [{"dataset_id": "flexivoice", "manifest_sha256": "m"}]
    rows = selection.audio_content_exclusions(rules, inputs)
    assert sorted(r["scope"]["audio_sha256"] for r in rows) == ["h1", "h2"]
    assert {r["reason"] for r in rows} == {103}
    assert rows[0]["evidence"]["record"] == {"sample_id": "a", "header_bytes": "4096"}
    rules["audio_content_evidence"][0]["expected_audio"] = 3
    with pytest.raises(ValueError, match="scope changed"):
        selection.audio_content_exclusions(rules, inputs)
    rules["audio_content_evidence"][0].update(expected_audio=2, sha256="0" * 64)
    with pytest.raises(ValueError, match="evidence changed"):
        selection.audio_content_exclusions(rules, inputs)
