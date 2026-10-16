"""References that start at the sample's first codec frame, with the uniform policy's lengths.

The length of every reference is the one `reference.sample` draws for the same identity and
seed (its RNG key keeps the uniform policy version), so the two policies differ only in the
start. Only rates with an integral number of samples per 80 ms are defined: there every
legal length is legal at start 0. `reference.py`, `profile.py` and `storage.py` stay
byte-identical, so the uniform profile and its acceptance remain reproducible.
"""

from pathlib import Path

import pyarrow as pa

from . import reference

POLICY_VERSION = "codec-grid-prefix-reference-v1"
PROFILE_NAME = "qwen3-ecapa-24k-fp32-prefix-reference-v1"
MERGED_PROFILE_NAME = "prefix-reference-features-v1"
SPEAKER_MODE = "codec_grid_prefix_reference"


def policy(seed):
    return dict(
        reference.policy(seed),
        version=POLICY_VERSION,
        sampling="uniform_legal_frame_length_then_start_zero",
        length_source=dict(
            policy_version=reference.POLICY_VERSION,
            rule="length of reference.sample for the same seed and identity",
        ),
        native_rates="integral_samples_per_80ms_only",
    )


def is_prefix(definition):
    return definition.get("reference", {}).get("version") == POLICY_VERSION


def profile(model_root, seed):
    from ...feature_runtime import file_hash
    from .profile import profile as uniform_profile

    definition = uniform_profile(model_root, seed)
    definition["reference"] = policy(seed)
    definition["implementation"]["sources_sha256"]["prefix_reference"] = file_hash(Path(__file__))
    definition["waveform"]["order"] = [
        "crop_codec_grid_prefix_reference" if step == "crop_codec_grid_reference" else step
        for step in definition["waveform"]["order"]
    ]
    return definition


def shift(coordinates, rate):
    """Move uniform coordinates to start 0, keeping their codec and native lengths."""
    if (2 * rate) % 25:
        raise NotImplementedError(f"Prefix references need integral 80 ms frames, not {rate} Hz")
    length = coordinates["reference_codec_end"] - coordinates["reference_codec_start"]
    end_frame = reference.boundary(length, rate)
    if end_frame != coordinates["end_frame"] - coordinates["start_frame"]:
        raise AssertionError("Prefix reference length differs from the uniform one")
    return dict(
        reference_codec_start=0, reference_codec_end=length, start_frame=0, end_frame=end_frame
    )


def sample(native_frames, rate, identity, seed):
    """The uniform policy's coordinates moved to start 0, or None when it has none."""
    uniform = reference.sample(native_frames, rate, identity, seed)
    return None if uniform is None else shift(uniform, rate)


def validate(row, codec):
    reference.validate(row, codec)
    if row["reference_codec_start"] != 0 or row["start_frame"] != 0:
        raise ValueError("Prefix reference does not start at the first frame")


def attach(rows, source, task, state, seed, dataset_id, release_id):
    """Persist the uniform plan (the lengths' source) and the prefix plan used for inference.

    `reference.attach` checks the pinned codec identity and keeps its plan under
    `<state>/uniform/references`; the prefix plan is `<state>/references`, as for uniform runs.
    """
    import pyarrow.ipc as ipc

    reference.attach(rows, source, task, Path(state) / "uniform", seed, dataset_id, release_id)
    plans = []
    for row in rows:
        uniform = row["reference"]
        ref = dict(uniform)
        if uniform["error_code"] is None:
            ref.update(shift(uniform, row["sample_rate"]))
        plans.append(ref)
        row["reference"] = ref
    plan_schema = pa.schema(
        [
            pa.field("target_id", pa.string()),
            *reference.FIELDS,
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
