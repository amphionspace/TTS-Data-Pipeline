"""One result per successful target, with native-sample low-energy intervals."""

import pyarrow as pa

from ...contract import annotation_sample_schema


def result_schema():
    floats = ["peak", "rms", "near_full_scale_ratio", "at_or_above_full_scale_ratio", "zero_ratio"]
    channel = pa.struct(
        [
            pa.field("channel", pa.int32()),
            *[pa.field(k, pa.float64()) for k in [*floats, "dc_offset"]],
        ]
    )
    interval = pa.struct([pa.field("start_sample", pa.int64()), pa.field("end_sample", pa.int64())])
    return pa.schema(
        [
            *[
                pa.field(k, pa.string())
                for k in ["target_kind", "target_id", "input_fingerprint", "audio_sha256"]
            ],
            *[
                pa.field(k, pa.int64())
                for k in [
                    "item_id",
                    "decoded_num_samples",
                    "start_sample",
                    "end_sample",
                    "declared_num_samples",
                    "num_samples_delta",
                    "leading_low_energy_samples",
                    "trailing_low_energy_samples",
                    "longest_low_energy_samples",
                ]
            ],
            *[
                pa.field(k, pa.int32())
                for k in [
                    "native_sample_rate",
                    "channels",
                    "declared_sample_rate",
                    "declared_channels",
                ]
            ],
            *[pa.field(k, pa.float64()) for k in [*floats, "duration_seconds", "low_energy_ratio"]],
            pa.field("header_format_matches", pa.bool_()),
            pa.field("channel_stats", pa.list_(channel)),
            pa.field("low_energy_intervals", pa.list_(interval)),
        ]
    )


def annotation_schema():
    """Published one-row-per-sample table, including failed targets."""
    identity = {"target_kind", "target_id", "input_fingerprint", "item_id"}
    payload = pa.struct([field for field in result_schema() if field.name not in identity])
    return annotation_sample_schema(payload)
