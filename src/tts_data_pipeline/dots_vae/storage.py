"""Columnar posterior and x-vector arrays built from flat buffers, with full validation."""

import numpy as np
import pyarrow as pa

from ..contract import dots_vae_schema
from .profile import LATENT_DIM, XVECTOR_DIM, feature_row, validate_row

ARRAYS = ("vae_mean", "vae_logstd", "xvector", "reference_xvector")


def latent_column(blocks, missing):
    """list<fixed_size_list<float32,D>> from per-row [T,D] arrays; None rows are null."""
    lengths = [0 if b is None else len(b) for b in blocks]
    offsets = np.zeros(len(blocks) + 1, dtype=np.int32)
    np.cumsum(lengths, out=offsets[1:])
    present = [b for b in blocks if b is not None]
    flat = (
        np.ascontiguousarray(np.concatenate(present), dtype=np.float32).reshape(-1)
        if present
        else np.zeros(0, np.float32)
    )
    inner = pa.FixedSizeListArray.from_arrays(pa.array(flat), LATENT_DIM)
    return pa.ListArray.from_arrays(
        pa.array(offsets), inner, mask=pa.array(missing), type=pa.list_(inner.type)
    )


def vector_column(vectors):
    """fixed_size_list<float32,512>; None entries are null."""
    flat = np.zeros((len(vectors), XVECTOR_DIM), dtype=np.float32)
    for i, vector in enumerate(vectors):
        if vector is not None:
            flat[i] = vector
    return pa.FixedSizeListArray.from_arrays(
        pa.array(flat.reshape(-1)),
        XVECTOR_DIM,
        mask=pa.array(np.array([v is None for v in vectors], dtype=bool)),
    )


def make_table(rows, values, definition, profile_id):
    """`values` are Encoder.extract tuples (info, posterior, xvector, reference_xvector, error)."""
    schema = dots_vae_schema(LATENT_DIM, XVECTOR_DIM)
    records = [
        feature_row(row, definition, info, error, profile_id, posterior, xvector, reference)
        for row, (info, posterior, xvector, reference, error) in zip(rows, values, strict=True)
    ]
    missing = np.array([v[1] is None for v in values], dtype=bool)
    means = [None if v[1] is None else v[1][:, :LATENT_DIM] for v in values]
    logstds = [None if v[1] is None else v[1][:, LATENT_DIM:] for v in values]
    metadata = pa.Table.from_pylist(
        records, schema=pa.schema([f for f in schema if f.name not in ARRAYS])
    )
    columns = dict(
        vae_mean=latent_column(means, missing),
        vae_logstd=latent_column(logstds, missing),
        xvector=vector_column([v[2] for v in values]),
        reference_xvector=vector_column([v[3] for v in values]),
    )
    return pa.Table.from_arrays(
        [columns[f.name] if f.name in ARRAYS else metadata[f.name] for f in schema],
        schema=schema,
    )


def row_arrays(column, i):
    value = column[i]
    if not value.is_valid:
        return None
    return value.values.flatten().to_numpy(zero_copy_only=False).reshape(-1, LATENT_DIM)


def vector_rows(column):
    vectors = column.chunk(0)
    flat = vectors.values.to_numpy(zero_copy_only=False).reshape(-1, XVECTOR_DIM)
    nulls = vectors.is_null().to_numpy(zero_copy_only=False)
    return [None if null else vector for null, vector in zip(nulls, flat, strict=True)]


def validate_table(table, rule):
    if table.schema != dots_vae_schema(LATENT_DIM, XVECTOR_DIM):
        raise ValueError("Unexpected dots_vae schema")
    table = table.combine_chunks()
    mean, logstd = table["vae_mean"].chunk(0), table["vae_logstd"].chunk(0)
    xvectors, references = vector_rows(table["xvector"]), vector_rows(table["reference_xvector"])
    for i, row in enumerate(table.drop(list(ARRAYS)).to_pylist()):
        validate_row(
            row, row_arrays(mean, i), row_arrays(logstd, i), xvectors[i], references[i], rule
        )
