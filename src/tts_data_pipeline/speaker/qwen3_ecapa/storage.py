"""Columnar FP32 vectors with complete identity and payload validation."""

import numpy as np
import pyarrow as pa

from ...contract import speaker_embedding_schema
from ...schema import digest
from .profile import feature_row, validate_result


def make_table(rows, values, definition):
    pid = digest(definition)
    dimension = definition["output"]["embedding_dim"]
    records = [
        feature_row(row, definition, info, vector, error, profile_id=pid, include_embedding=False)
        for row, (info, vector, error) in zip(rows, values, strict=True)
    ]
    vectors = np.zeros((len(rows), dimension), dtype=np.float32)
    missing = np.ones(len(rows), dtype=bool)
    for i, (_, vector, error) in enumerate(values):
        if vector is not None:
            vectors[i] = vector
            missing[i] = False
    if "reference" in definition:
        from .reference import schema as reference_schema

        schema = reference_schema(dimension)
    else:
        schema = speaker_embedding_schema(dimension)
    metadata_schema = pa.schema([field for field in schema if field.name != "embedding"])
    metadata = pa.Table.from_pylist(records, schema=metadata_schema)
    embedding = pa.FixedSizeListArray.from_arrays(
        pa.array(vectors.reshape(-1)), dimension, mask=pa.array(missing)
    )
    table = pa.Table.from_arrays(
        [embedding if field.name == "embedding" else metadata[field.name] for field in schema],
        schema=schema,
    )
    return table


def validate_table(table):
    table = table.combine_chunks()
    returned = table["embedding"].chunk(0)
    actual = returned.values.to_numpy(zero_copy_only=False).reshape(-1, returned.type.list_size)
    metadata = table.drop(["embedding"]).to_pylist()
    nulls = returned.is_null().to_numpy(zero_copy_only=False)
    for i, row in enumerate(metadata):
        row["embedding"] = None if nulls[i] else actual[i]
        validate_result(row)
        if "reference_codec_start" in row and row["status"] == "ok":
            from .reference import validate

            total, rate = row["reference_native_total_frames"], row["native_sample_rate"]
            validate(
                row,
                dict(
                    target_id=row["target_id"],
                    audio_sha256=row["audio_sha256"],
                    native_sample_rate=rate,
                    start_frame=0,
                    end_frame=total,
                    feature_key=row["reference_codec_feature_key"],
                    num_codec_frames=((total * 24000 + rate - 1) // rate + 1919) // 1920,
                ),
            )
