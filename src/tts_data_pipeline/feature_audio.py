"""Canonical JSON and typed-array hashing shared by audio features."""

import hashlib
import json

import numpy as np


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def array_sha256(array, axes):
    array = np.asarray(array)
    if array.dtype.kind not in "fi" or array.ndim != len(axes):
        raise ValueError("Unsupported canonical array")
    data = np.ascontiguousarray(array, dtype=array.dtype.newbyteorder("<"))
    header = {"dtype": data.dtype.name, "shape": list(data.shape), "axes": axes}
    return hashlib.sha256(canonical(header) + b"\n" + data.tobytes()).hexdigest()
