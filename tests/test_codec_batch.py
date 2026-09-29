from types import SimpleNamespace

import pytest
import torch
from transformers import MimiConfig
from transformers.models.mimi.modeling_mimi import MimiEuclideanCodebook

from tts_data_pipeline.codec_batch import batch_indices, batch_shape, install_c


def test_canonical_shapes_cover_each_input_once():
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


def test_cold_install_retains_fp32_normalized_codebook_cache():
    model = torch.nn.Module()
    model.config = SimpleNamespace()
    model.encoder_transformer = torch.nn.Module()
    model.encoder_transformer.layers = torch.nn.ModuleList()
    model.codebook = MimiEuclideanCodebook(MimiConfig(codebook_size=4, codebook_dim=3))
    model.codebook.embed_sum.copy_(torch.arange(12).reshape(4, 3) / 13)
    model.codebook.cluster_usage.copy_(torch.tensor([1.1, 1.7, 2.3, 4.1]))
    expected = model.codebook.embed_sum / model.codebook.cluster_usage[:, None]
    assert model.codebook._embed is None
    install_c(model, "cpu")
    assert model.codebook.embed_sum.dtype == torch.float16
    assert model.codebook._embed.dtype == torch.float32
    assert torch.equal(model.codebook._embed, expected)
    assert not torch.equal(expected.half().float(), expected)
