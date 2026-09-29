"""Arrow type definitions for the v0.1 contract; task executors are separate."""

import pyarrow as pa

from .schema import VERSION, base_schema


def annotation_type(result_type):
    return pa.struct(
        [
            pa.field("status", pa.string()),
            pa.field("input_fingerprint", pa.string()),
            pa.field("error_code", pa.string()),
            pa.field("result", result_type),
        ]
    )


def annotation_targets_schema():
    return pa.schema(
        [
            *[
                pa.field(k, pa.string())
                for k in ("target_kind", "target_id", "input_fingerprint", "status", "error_code")
            ],
            pa.field("item_count", pa.int64()),
        ]
    )


def view_schema():
    strings = [
        "view_id",
        "view_revision",
        "view_run_id",
        "source_view_key",
        "view_kind",
        "parent_sample_id",
        "parent_audio_sha256",
        "timeline_profile_id",
        "text",
        "text_kind",
        "language",
        "speaker_id",
        "speaker_scope",
        "recording_id",
        "group_id",
        "parent_view_id",
        "metadata_json",
    ]
    return pa.schema(
        [
            *[pa.field(k, pa.string()) for k in strings],
            pa.field("start_frame", pa.int64()),
            pa.field("end_frame", pa.int64()),
        ]
    )


def codec_schema(num_codebooks, dtype="int16"):
    if num_codebooks < 1 or dtype not in {"int16", "int32"}:
        raise ValueError("Codec requires a positive codebook count and int16/int32 dtype")
    strings = [
        "target_kind",
        "target_id",
        "parent_sample_id",
        "feature_key",
        "input_fingerprint",
        "profile_id",
        "audio_sha256",
        "timeline_profile_id",
        "status",
        "error_code",
        "codes_sha256",
        "encoder_input_sha256",
    ]
    return pa.schema(
        [
            *[pa.field(k, pa.string()) for k in strings],
            pa.field("start_frame", pa.int64()),
            pa.field("end_frame", pa.int64()),
            pa.field("native_sample_rate", pa.int32()),
            pa.field("encoder_input_num_frames", pa.int64()),
            pa.field("num_codec_frames", pa.int64()),
            pa.field("num_codebooks", pa.int32()),
            pa.field("codes", pa.list_(pa.list_(pa.type_for_alias(dtype), num_codebooks))),
        ]
    )


def speaker_embedding_schema(dimension):
    if dimension < 1:
        raise ValueError("Speaker embedding requires a positive dimension")
    strings = [
        "target_kind",
        "target_id",
        "parent_sample_id",
        "feature_key",
        "input_fingerprint",
        "profile_id",
        "audio_sha256",
        "timeline_profile_id",
        "status",
        "error_code",
        "encoder_input_sha256",
        "embedding_sha256",
    ]
    return pa.schema(
        [
            *[pa.field(k, pa.string()) for k in strings],
            pa.field("start_frame", pa.int64()),
            pa.field("end_frame", pa.int64()),
            pa.field("native_sample_rate", pa.int32()),
            pa.field("encoder_input_num_frames", pa.int64()),
            pa.field("embedding_dim", pa.int32()),
            pa.field("embedding", pa.list_(pa.float32(), dimension)),
        ]
    )


def feature_targets_schema():
    return pa.schema(
        [
            pa.field(k, pa.string(), nullable=False)
            for k in ("target_kind", "target_id", "parent_sample_id")
        ]
    )


def selection_columns_schema():
    return pa.schema(
        [
            pa.field("selection_reason", pa.uint16(), nullable=False),
            pa.field("selection_flags", pa.uint32(), nullable=False),
        ]
    )


def type_description(dtype):
    if pa.types.is_struct(dtype):
        return {"type": "struct", "fields": [field_description(f) for f in dtype]}
    if pa.types.is_list(dtype) or pa.types.is_fixed_size_list(dtype):
        result = {"type": "list", "item": type_description(dtype.value_type)}
        if pa.types.is_fixed_size_list(dtype):
            result["size"] = dtype.list_size
        return result
    return {"type": str(dtype)}


def field_description(field):
    return {"name": field.name, "nullable": field.nullable, **type_description(field.type)}


def schema_description(schema):
    return [field_description(f) for f in schema]


def contract_types():
    return {
        "contract_version": VERSION,
        "representation": "Arrow type descriptors; semantic validation is additional",
        "base": schema_description(base_schema()),
        "view": schema_description(view_schema()),
        "annotation_targets": schema_description(annotation_targets_schema()),
        "feature_targets": schema_description(feature_targets_schema()),
        "selection_columns": schema_description(selection_columns_schema()),
        "selection_text_columns": schema_description(
            pa.schema(
                [
                    pa.field("selected_text", pa.string()),
                    pa.field("selected_text_source", pa.uint32()),
                ]
            )
        ),
        "quality_example": type_description(
            annotation_type(pa.struct([pa.field("score", pa.float64())]))
        ),
        "codec_example": {
            "example_only": True,
            "note": "K=2 is a storage example, not the selected Qwen tokenizer layout",
            "fields": schema_description(codec_schema(2)),
        },
        "speaker_embedding_example": {
            "example_only": True,
            "note": "D=3 illustrates storage; actual D comes from the selected speaker encoder",
            "fields": schema_description(speaker_embedding_schema(3)),
        },
    }
