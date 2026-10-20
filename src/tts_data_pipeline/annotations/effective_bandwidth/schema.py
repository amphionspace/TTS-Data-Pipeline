"""Published one-row-per-sample effective bandwidth, including failed targets."""

import pyarrow as pa

from ...contract import annotation_sample_schema

SCHEMA_VERSION = "effective-bandwidth-v1"


def result_type():
    return pa.struct(
        [
            pa.field("audio_sha256", pa.string()),
            pa.field("native_sample_rate", pa.int32()),
            pa.field("duration_seconds", pa.float64()),
            pa.field("num_samples_delta", pa.int64()),
            pa.field("container", pa.string()),
            pa.field("encoded_kbps", pa.float64()),
            pa.field("segment_samples", pa.int32()),
            pa.field("nyquist_hz", pa.float64()),
            pa.field("reference_level_db", pa.float64()),
            *[pa.field(f"edge_hz_{n}", pa.float64()) for n in (40, 50, 60)],
            *[
                pa.field(f"band_db_{k}", pa.float64())
                for k in ("4_8k", "8_12k", "12_16k", "16k_up")
            ],
            pa.field("edge_drop_db", pa.float64()),
            pa.field("decoder_warnings", pa.int32()),
        ]
    )


def annotation_schema():
    return annotation_sample_schema(result_type())
