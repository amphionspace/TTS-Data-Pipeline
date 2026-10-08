"""Deterministic references on the complete codec's 80 ms supervision grid.

Coordinates describe nominal frame times, not the codec's receptive field.
Native boundaries use ceil(k * 1920 * rate / 24000), with no partial tail frame.
"""

import hashlib
import math

import pyarrow as pa

from ...contract import speaker_embedding_schema
from ...schema import digest

POLICY_VERSION = "codec-grid-reference-v1"
PROFILE_NAME = "qwen3-ecapa-24k-fp32-reference-v1"
FIELDS = [
    pa.field("reference_codec_start", pa.int64()),
    pa.field("reference_codec_end", pa.int64()),
    pa.field("reference_codec_feature_key", pa.string()),
    pa.field("reference_native_total_frames", pa.int64()),
]


def policy(seed):
    return dict(
        version=POLICY_VERSION,
        seed=seed,
        min_seconds=0.5,
        min_fraction=0.1,
        max_fraction=0.5,
        codec_hop_samples=1920,
        codec_sample_rate=24000,
        interval="half_open",
        native_boundary="ceil(codec_boundary * 2 * native_rate / 25)",
        partial_tail="excluded_from_reference_only",
        sampling="uniform_legal_frame_length_then_uniform_legal_start",
        rng="sha256_counter_rejection_u256_v1",
    )


def schema(dimension):
    return pa.schema([*speaker_embedding_schema(dimension), *FIELDS])


def boundary(frame, rate):
    return (frame * 2 * rate + 24) // 25


class Random:
    def __init__(self, identity):
        self.key = bytes.fromhex(digest(identity))
        self.counter = 0

    def below(self, n):
        if n < 1:
            raise ValueError("Empty random range")
        limit = (1 << 256) - (1 << 256) % n
        while True:
            value = int.from_bytes(
                hashlib.sha256(self.key + self.counter.to_bytes(8, "big")).digest(), "big"
            )
            self.counter += 1
            if value < limit:
                return value % n


def sample(native_frames, rate, identity, seed):
    """Return integer coordinates, or None. All constraints apply after rounding."""
    if native_frames < 1 or rate < 1:
        raise ValueError("Invalid native timeline")
    lo = max((rate + 1) // 2, (native_frames + 9) // 10)
    hi = native_frames // 2
    if lo > hi:
        return None
    full = native_frames * 25 // (2 * rate)
    rng = Random([POLICY_VERSION, seed, identity])
    # All normal audio rates have an integral number of samples per 80 ms.
    if (2 * rate) % 25 == 0:
        hop = 2 * rate // 25
        low, high = (lo + hop - 1) // hop, hi // hop
        if low > high:
            return None
        length = low + rng.below(high - low + 1)
        start = rng.below(full - length + 1)
    else:
        # Rounding repeats every <=25 frame boundaries. Enumerate residues, not
        # every possible start, and sample starts uniformly by their counts.
        period = 25 // math.gcd(2 * rate, 25)
        candidates = []
        for length in range(max(1, (lo - 1) * 25 // (2 * rate)), hi * 25 // (2 * rate) + 2):
            groups = []
            for residue in range(min(period, full - length + 1)):
                size = boundary(residue + length, rate) - boundary(residue, rate)
                if lo <= size <= hi:
                    groups.append((residue, (full - length - residue) // period + 1))
            if groups:
                candidates.append((length, groups))
        if not candidates:
            return None
        length, groups = candidates[rng.below(len(candidates))]
        pick = rng.below(sum(count for _, count in groups))
        for residue, count in groups:
            if pick < count:
                start = residue + pick * period
                break
            pick -= count
    end = start + length
    a, b = boundary(start, rate), boundary(end, rate)
    assert 0 <= a < b <= native_frames and lo <= b - a <= hi
    return dict(
        reference_codec_start=start,
        reference_codec_end=end,
        start_frame=a,
        end_frame=b,
    )


def validate(row, codec):
    """Verify coordinates and the exact codec row to which they belong."""
    rate, total = codec["native_sample_rate"], codec["end_frame"]
    a, b = row["reference_codec_start"], row["reference_codec_end"]
    start, end = row["start_frame"], row["end_frame"]
    if (
        row["reference_codec_feature_key"] != codec["feature_key"]
        or row["reference_native_total_frames"] != total
        or row["native_sample_rate"] != rate
        or row["audio_sha256"] != codec["audio_sha256"]
        or row["target_id"] != codec["target_id"]
        or codec["start_frame"] != 0
        or not 0 <= a < b <= total * 25 // (2 * rate)
        or b > codec["num_codec_frames"]
        or (start, end) != (boundary(a, rate), boundary(b, rate))
        or not max((rate + 1) // 2, (total + 9) // 10) <= end - start <= total // 2
    ):
        raise ValueError("Invalid reference-to-codec mapping")


def attach(rows, source, task, state, seed, dataset_id, release_id):
    """Persist each task's reference plan before inference; retries verify it exactly."""
    from pathlib import Path

    import pyarrow.ipc as ipc

    from ...feature_runtime import align, read_aligned, source_table

    columns = [
        "target_id",
        "audio_sha256",
        "status",
        "native_sample_rate",
        "start_frame",
        "end_frame",
        "num_codec_frames",
        "encoder_input_num_frames",
        "feature_key",
    ]
    # Codec order follows selection tasks, but the ID lookup remains authoritative.
    ds = source_table(source)
    ids = pa.chunked_array([[r["sample_id"] for r in rows]])
    table = read_aligned(ds, dict(offset=task["reference_offset"], rows=len(rows)), ids, columns)
    codecs = align(table, ids).to_pylist()
    plans = []
    for row, codec in zip(rows, codecs, strict=True):
        total, rate = codec["end_frame"], codec["native_sample_rate"]
        if (
            codec["status"] != "ok"
            or codec["start_frame"] != 0
            or codec["audio_sha256"] != row["audio_sha256"]
            or rate != row["sample_rate"]
            or codec["encoder_input_num_frames"] != (total * 24000 + rate - 1) // rate
            or codec["num_codec_frames"] != (codec["encoder_input_num_frames"] + 1919) // 1920
        ):
            raise ValueError("Pinned codec identity or timeline conflict")
        coordinates = sample(
            total, rate, [dataset_id, release_id, row["sample_id"], row["audio_sha256"]], seed
        )
        ref = dict(
            target_id=row["sample_id"],
            reference_codec_feature_key=codec["feature_key"],
            reference_native_total_frames=total,
            error_code=None if coordinates else "no_legal_reference",
            **(
                coordinates
                or dict(
                    start_frame=0, end_frame=0, reference_codec_start=None, reference_codec_end=None
                )
            ),
        )
        plans.append(ref)
        row["reference"] = ref
    plan_schema = pa.schema(
        [
            pa.field("target_id", pa.string()),
            *FIELDS,
            pa.field("start_frame", pa.int64()),
            pa.field("end_frame", pa.int64()),
            pa.field("error_code", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(plans, schema=plan_schema)
    path = Path(state) / "references" / f"{task['id']}.arrow"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with ipc.open_file(path) as reader:
            if not reader.read_all().equals(table):
                raise ValueError("Saved reference plan changed")
    else:
        temporary = path.with_suffix(".tmp")
        with ipc.new_file(temporary, table.schema) as writer:
            writer.write_table(table)
        temporary.replace(path)
    return path
