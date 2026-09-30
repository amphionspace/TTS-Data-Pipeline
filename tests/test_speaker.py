import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import soundfile as sf
import torch
import torchaudio.functional as AF

from tts_data_pipeline.speaker.qwen3_ecapa.audio import MEL_OPTIONS, decode, mel_spectrogram
from tts_data_pipeline.speaker.qwen3_ecapa.encoder import Encoder, batch_indices
from tts_data_pipeline.speaker.qwen3_ecapa.profile import feature_row, validate_result


def row(samples, rate=24000, channels=1):
    wave = np.random.default_rng(samples).normal(0, 0.1, (samples, channels)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, wave, rate, format="WAV", subtype="FLOAT")
    raw = buffer.getvalue()
    return dict(
        sample_id=hashlib.sha256(raw).hexdigest(),
        audio={"bytes": raw},
        audio_sha256=hashlib.sha256(raw).hexdigest(),
        num_frames=samples,
        sample_rate=rate,
        channels=channels,
    )


@pytest.mark.parametrize("rate,channels", [(16000, 2), (24000, 1), (44100, 2)])
def test_frontend_matches_training_operations(rate, channels):
    source = row(rate, rate, channels)
    mel, info, error = decode(source)
    audio, sr = sf.read(io.BytesIO(source["audio"]["bytes"]), dtype="float32", always_2d=True)
    wave = torch.from_numpy(np.ascontiguousarray(audio.mean(axis=1)))
    if sr != 24000:
        wave = AF.resample(wave, sr, 24000)
    expected = mel_spectrogram(wave[None], **MEL_OPTIONS)[0].T.contiguous()
    assert error is None and info["encoder_input_num_frames"] == 24000
    assert torch.equal(mel, expected)


def test_short_audio_is_failure_without_padding():
    source = row(1279)
    mel, info, error = decode(source)
    assert mel is None and error == "speaker_input_too_short"
    definition = dict(timeline_profile_id="a" * 64, output=dict(embedding_dim=1024))
    record = feature_row(source, definition, info, error=error)
    validate_result(record)
    assert record["embedding"] is None and record["encoder_input_num_frames"] == 1279
    assert decode(row(1280))[0].shape == (5, 128)


def test_integrity_failure_is_not_swallowed():
    source = row(24000)
    with pytest.raises(ValueError, match="audio_sha256_mismatch"):
        decode({**source, "audio_sha256": "0" * 64})
    mel, info, error = decode({**source, "sample_rate": 16000})
    assert mel is None and error == "native_format_mismatch"
    assert info["encoder_input_num_frames"] is None


def test_batches_have_equal_actual_lengths_and_preserve_every_sample():
    sources = [
        row(n, sr)
        for n, sr in [(16000, 16000), (24000, 24000), (24063, 24000), (25600, 24000), (1279, 24000)]
    ]
    batches = list(batch_indices(sources))
    assert sorted(i for batch in batches for i in batch) == list(range(len(sources)))
    for batch in batches:
        lengths = [decode(sources[i])[0].shape[0] for i in batch if decode(sources[i])[2] is None]
        assert len(set(lengths)) <= 1
    assert any(len(batch) == 3 for batch in batches)


def test_extract_restores_order_and_rejects_mixed_forward():
    encoder = Encoder.__new__(Encoder)
    encoder.device = torch.device("cpu")
    encoder.dimension = 128
    encoder.model = lambda x: x.mean(dim=1)
    sources = [row(25600), row(1279), row(24000), row(24001)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        output = encoder.extract(sources, pool)
    for source, (info, vector, error) in zip(sources, output, strict=True):
        assert info["encoder_input_num_frames"] == source["num_frames"]
        mel, _, expected_error = decode(source)
        assert error == expected_error
        if error is None:
            np.testing.assert_allclose(vector, mel.mean(dim=0).numpy(), atol=1e-6)
    with pytest.raises(ValueError, match="identical lengths"):
        encoder.encode([torch.ones(93, 128), torch.ones(94, 128)])


def test_native_resampler_rounding_is_not_corrected_or_padded():
    from tts_data_pipeline.speaker.qwen3_ecapa.audio import resampled_frames

    # Non-integer rational length rounds down to an integer in torchaudio FP32.
    frames = next(
        n
        for n in range(240000, 250000)
        if resampled_frames(n, 44100) != (n * 24000 + 44099) // 44100
    )
    source = row(frames, 44100)
    mel, info, error = decode(source)
    audio, _ = sf.read(io.BytesIO(source["audio"]["bytes"]), dtype="float32")
    wave = AF.resample(torch.from_numpy(audio), 44100, 24000)
    assert info["encoder_input_num_frames"] == len(wave) == resampled_frames(frames, 44100)
    assert len(wave) == (frames * 24000 + 44099) // 44100 - 1
    assert error is None
    assert torch.equal(mel, mel_spectrogram(wave[None], **MEL_OPTIONS)[0].T.contiguous())


@pytest.mark.parametrize("delta", [-10, -2, -1, 1, 2, 10])
def test_small_container_frame_delta_keeps_actual_waveform(delta):
    source = row(24063)
    expected, expected_info, _ = decode(source)
    mel, info, error = decode({**source, "num_frames": source["num_frames"] + delta})
    assert error is None and info == expected_info
    assert torch.equal(mel, expected)


def test_actual_mel_lengths_override_metadata_grouping():
    encoder = Encoder.__new__(Encoder)
    encoder.device = torch.device("cpu")
    encoder.dimension = 128
    shapes = []

    def model(batch):
        shapes.append(tuple(batch.shape))
        return batch.mean(dim=1)

    encoder.model = model
    first = row(24063)
    sources = [{**first, "num_frames": 24064}, row(24064)]
    assert list(batch_indices(sources)) == [[0, 1]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        output = encoder.extract(sources, pool)
    assert shapes == [(1, 93, 128), (1, 94, 128)]
    assert all(error is None for _, _, error in output)


def test_feature_interval_uses_actual_native_decode():
    source = row(24063)
    declared = {**source, "num_frames": 24064}
    _, info, error = decode(declared)
    definition = dict(timeline_profile_id="a" * 64, output=dict(embedding_dim=1024))
    record = feature_row(declared, definition, info, np.ones(1024, dtype=np.float32), error)
    assert record["end_frame"] == 24063
    assert record["encoder_input_num_frames"] == 24063
    validate_result(record)
