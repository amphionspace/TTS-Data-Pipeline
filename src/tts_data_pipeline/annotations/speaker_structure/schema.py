"""Frozen result schema of the MOSS speaker_structure annotation (spec 05)."""

import pyarrow as pa

from ...contract import annotation_sample_schema

TURN = pa.struct(
    [
        pa.field("speaker", pa.string()),
        pa.field("start_sample", pa.int64()),
        pa.field("end_sample", pa.int64()),
        pa.field("text", pa.string()),
    ]
)
INTERVAL = pa.struct([pa.field("start_sample", pa.int64()), pa.field("end_sample", pa.int64())])


def result_type():
    return pa.struct(
        [
            pa.field("audio_sha256", pa.string()),
            pa.field("sample_rate", pa.int32()),
            pa.field("num_frames", pa.int64()),
            pa.field("speaker_count", pa.int32()),
            pa.field("turn_union_samples", pa.int64()),
            pa.field("overlap_samples", pa.int64()),
            pa.field("overlap_ratio", pa.float64()),
            pa.field("dominant_speaker_ratio", pa.float64()),
            pa.field("turns", pa.list_(TURN)),
            pa.field("overlaps", pa.list_(INTERVAL)),
            pa.field("response_sha256", pa.string()),
            pa.field("response_bytes", pa.int64()),
        ]
    )


def annotation_schema():
    return annotation_sample_schema(result_type())


def checkpoint_schema():
    """Private per-fragment checkpoint: the published columns plus resume evidence."""
    return pa.schema(
        list(annotation_schema())
        + [
            pa.field("round", pa.int32(), nullable=False),
            pa.field("attempts", pa.int32(), nullable=False),
            pa.field("raw_response", pa.string()),
        ]
    )
