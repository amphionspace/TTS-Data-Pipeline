"""One row per input sample, including failures; no second target table."""

import pyarrow as pa

from ...contract import annotation_sample_schema


def annotation_schema():
    return annotation_sample_schema(
        pa.struct(
            [
                pa.field("audio_sha256", pa.string()),
                pa.field("source_language", pa.string()),
                pa.field("native_sample_rate", pa.int32()),
                pa.field("channels", pa.int32()),
                pa.field("decoded_num_samples", pa.int64()),
                pa.field("declared_num_samples", pa.int64()),
                pa.field("start_sample", pa.int64()),
                pa.field("end_sample", pa.int64()),
                pa.field("model_sample_rate", pa.int32()),
                pa.field("model_num_samples", pa.int64()),
                pa.field("analysis_truncated", pa.bool_()),
                pa.field("raw_language", pa.string()),
                pa.field("language", pa.string()),
                pa.field("confidence", pa.float64()),
                pa.field("source_language_mismatch", pa.bool_()),
            ]
        )
    )
