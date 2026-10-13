"""Frozen result schema of the upstream DNSMOS import (spec 05)."""

import pyarrow as pa

from ...contract import annotation_sample_schema


def result_type():
    return pa.struct(
        [
            pa.field("audio_sha256", pa.string()),
            pa.field("upstream_dnsmos", pa.float64()),
        ]
    )


def annotation_schema():
    return annotation_sample_schema(result_type())
