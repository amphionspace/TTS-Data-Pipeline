"""Codec implementations, grouped by tokenizer.

Legacy public symbols resolve lazily to qwen3_12hz. New callers should import
from the named implementation. Metadata-only imports do not load audio/GPU deps.
"""

__all__ = [
    "Encoder",
    "array_sha256",
    "canonical",
    "feature_row",
    "profile",
    "validate_codes",
    "verified_model_sources",
    "waveform",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .qwen3_12hz import encoder

    value = getattr(encoder, name)
    globals()[name] = value
    return value
