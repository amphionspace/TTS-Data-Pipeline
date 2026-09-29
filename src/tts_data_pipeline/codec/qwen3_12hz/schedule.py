"""CPU length grouping for bounded work and memory; no waveform or tail padding."""

from collections import defaultdict


def batch_shape(length):
    if length <= 0 or length > 120 * 24000:
        raise ValueError("Codec supports positive audio lengths up to 120 seconds")
    frames = (length + 48000 - 1) // 48000 * 48000
    limit = max(1, min(64, 480 * 24000 // frames))
    return frames, 2 ** (limit.bit_length() - 1)


def batch_indices(lengths):
    buckets = defaultdict(list)
    for i, n in enumerate(lengths):
        buckets[batch_shape(n)].append(i)
    for (frames, size), indices in sorted(buckets.items()):
        for start in range(0, len(indices), size):
            yield frames, size, indices[start : start + size]
