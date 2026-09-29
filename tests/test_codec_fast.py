import pytest
import torch
from transformers import MimiConfig
from transformers.models.mimi.modeling_mimi import MimiConv1d

from tts_data_pipeline.codec_fast import integer_padding_forward


@pytest.mark.parametrize(
    "causal,pad,stride,dilation",
    [
        (True, "constant", 1, 1),
        (True, "constant", 4, 1),
        (True, "constant", 1, 3),
        (True, "replicate", 2, 1),
        (False, "constant", 3, 1),
        (False, "reflect", 2, 1),
    ],
)
def test_integer_padding_matches_reference(causal, pad, stride, dilation):
    torch.set_num_threads(1)
    config = MimiConfig(use_causal_conv=causal, pad_mode=pad)
    layer = MimiConv1d(config, 3, 4, 2 * stride + 1, stride=stride, dilation=dilation)
    for length in [1, 2, 3, 4, 7, 15, 32, 61, 1919, 1920, 1921]:
        values = torch.randn(1, 3, length)
        assert torch.equal(layer(values), integer_padding_forward(layer, values))
    with pytest.raises(ValueError, match="streaming"):
        integer_padding_forward(layer, values, padding_cache=object())
