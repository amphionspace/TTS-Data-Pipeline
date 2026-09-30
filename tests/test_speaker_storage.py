import hashlib
import io

import numpy as np
import pyarrow as pa
import pytest
import soundfile as sf
import torch
import torchaudio.functional as AF
from lance.file import LanceFileReader
from lance.fragment import write_fragments

from tts_data_pipeline.feature_audio import array_sha256
from tts_data_pipeline.speaker.qwen3_ecapa.frontend import decode_waveform
from tts_data_pipeline.speaker.qwen3_ecapa.profile import feature_row
from tts_data_pipeline.speaker.qwen3_ecapa.storage import make_table, validate_table


@pytest.mark.parametrize("all_failed", [False, True])
def test_fragments_preserve_vectors_nulls_and_identities(tmp_path, all_failed):
    definition = dict(timeline_profile_id="a" * 64, output=dict(embedding_dim=4))
    rows = [
        dict(sample_id=str(i) * 64, audio_sha256=str(i) * 64, sample_rate=24000, num_frames=24000)
        for i in range(3)
    ]
    info = dict(end_frame=23999, encoder_input_num_frames=23999, encoder_input_sha256="f" * 64)
    values = [
        (info, None, "invalid_native_waveform")
        if all_failed or i == 1
        else (info, np.array([1.0, -0.5, 0.25, 0.0], dtype=np.float32), None)
        for i in range(3)
    ]
    table = make_table(rows, values, definition)
    destination = tmp_path / "features.lance"
    fragments = write_fragments(
        table, destination, schema=table.schema, mode="create", data_storage_version="2.2"
    )
    batches = []
    for fragment in fragments:
        for item in fragment.files:
            batches.extend(
                LanceFileReader(str(destination / "data" / item.path)).read_all().to_batches()
            )
    back = pa.Table.from_batches(batches, schema=table.schema)
    assert back.equals(table)
    assert back.to_pylist() == [feature_row(r, definition, *v) for r, v in zip(rows, values)]
    validate_table(back)


def test_packed_frontend_keeps_native_resample_and_actual_frames():
    raw = io.BytesIO()
    native = np.random.default_rng(0).normal(0, 0.1, (24063, 2)).astype(np.float32)
    sf.write(raw, native, 44100, format="WAV", subtype="FLOAT")
    encoded = raw.getvalue()
    row = dict(
        audio={"bytes": encoded},
        audio_sha256=hashlib.sha256(encoded).hexdigest(),
        sample_rate=44100,
        channels=2,
        num_frames=24064,
    )
    wave, info, error = decode_waveform(row)
    reference = AF.resample(torch.from_numpy(native.mean(axis=1)), 44100, 24000)
    assert error is None and torch.equal(wave, reference)
    assert info["end_frame"] == 24063
    assert info["encoder_input_sha256"] == array_sha256(reference.numpy(), ["sample"])
