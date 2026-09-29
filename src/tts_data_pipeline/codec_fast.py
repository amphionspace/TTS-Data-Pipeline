"""Output-preserving non-streaming Mimi execution optimizations."""

import types


def integer_padding_forward(self, hidden_states, padding_cache=None):
    """Use Python shape integers instead of synchronizing CUDA scalar buffers per layer."""
    if padding_cache is not None:
        raise ValueError("Integer-padding codec path does not support streaming")
    stride = self.conv.stride[0]
    kernel = (self.conv.kernel_size[0] - 1) * self.conv.dilation[0] + 1
    total = kernel - stride
    extra = (-hidden_states.shape[-1]) % stride
    left, right = (total, extra) if self.causal else (total - total // 2, total // 2 + extra)
    return self.conv(self._pad1d(hidden_states, (left, right), mode=self.pad_mode))


def install_integer_padding(model):
    from transformers.models.mimi.modeling_mimi import MimiConv1d

    count = 0
    for layer in model.modules():
        if isinstance(layer, MimiConv1d):
            layer.forward = types.MethodType(integer_padding_forward, layer)
            count += 1
    return count
