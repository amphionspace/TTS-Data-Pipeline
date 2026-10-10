"""Task-specific result schema; target bookkeeping uses the shared contract."""

import pyarrow as pa

from ...contract import annotation_sample_schema


def result_schema():
    return pa.schema(
        [
            *[
                pa.field(name, pa.string())
                for name in (
                    "target_kind",
                    "target_id",
                    "input_fingerprint",
                    "audio_sha256",
                    "source_language",
                    "source_text_sha256",
                    "text",
                    "language",
                    "request_audio_sha256",
                    "raw_response_json",
                    "language_candidate_text",
                    "language_candidate_response_json",
                )
            ],
            pa.field("item_id", pa.int64()),
            pa.field("native_sample_rate", pa.int32()),
            pa.field("start_frame", pa.int64()),
            pa.field("end_frame", pa.int64()),
            pa.field("language_mismatch", pa.bool_()),
            pa.field("request_frames", pa.int64()),
        ]
    )


def annotation_schema():
    """Published one-row-per-sample table; checkpoints retain the legacy schema."""
    identity = {"target_kind", "target_id", "input_fingerprint", "item_id"}
    return annotation_sample_schema(
        pa.struct([field for field in result_schema() if field.name not in identity])
    )
