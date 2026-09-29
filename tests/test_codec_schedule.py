import pytest

from tts_data_pipeline.codec.qwen3_12hz.schedule import batch_indices, batch_shape


def test_length_groups_cover_each_input_once():
    assert batch_shape(24000) == batch_shape(48000) == (48000, 64)
    assert batch_shape(48001) == (96000, 64)
    assert batch_shape(2880000) == (2880000, 4)
    lengths = [48001, 24000, 2880000, 48000, 24001] * 70
    seen = []
    for frames, size, indices in batch_indices(lengths):
        assert len(indices) <= size
        assert all(batch_shape(lengths[i]) == (frames, size) for i in indices)
        seen.extend(indices)
    assert sorted(seen) == list(range(len(lengths)))
    for length in [0, -1, 2880001]:
        with pytest.raises(ValueError):
            batch_shape(length)
