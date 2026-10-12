"""Published one-row-per-sample audio statistics, including failed targets."""

import pyarrow as pa

from ...contract import annotation_sample_schema

SCHEMA_VERSION = "audio-stats-v1"


def result_type():
    return pa.struct(
        [
            pa.field("audio_sha256", pa.string()),
            pa.field("native_sample_rate", pa.int32()),
            pa.field("source_channels", pa.int32()),
            pa.field("decoded_num_samples", pa.int64()),
            pa.field("duration_seconds", pa.float64()),
            pa.field("num_samples_delta", pa.int64()),
            *[
                pa.field(k, pa.float64())
                for k in ("peak", "rms", "dc_offset", "near_full_scale_ratio", "low_energy_ratio")
            ],
            *[
                pa.field(k, pa.int64())
                for k in (
                    "leading_low_energy_samples",
                    "trailing_low_energy_samples",
                    "longest_low_energy_samples",
                    "longest_internal_low_energy_samples",
                )
            ],
            pa.field("window_rms_p10", pa.float64()),
            pa.field("window_rms_p90", pa.float64()),
            pa.field("longest_internal_zero_run_samples", pa.int64()),
            pa.field("decoder_warnings", pa.int32()),
        ]
    )


def annotation_schema():
    return annotation_sample_schema(result_type())
