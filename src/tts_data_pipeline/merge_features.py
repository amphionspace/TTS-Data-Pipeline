"""Join published feature snapshots by sample ID, preserving all three source tables."""

import fcntl
import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from lance.file import LanceFileReader
from lance.fragment import FragmentMetadata, write_fragments

from .contract import codec_text_schema, feature_targets_schema, schema_description
from .feature_contract import make_feature_run_id, validate_feature_coverage
from .feature_runtime import (
    align,
    event,
    file_hash,
    ordered_hash,
    read_aligned,
    sorted_set_hash,
    source_table,
    verify_checkpoint,
    write_json,
)
from .schema import digest
from .timestamps import BEIJING

KINDS = ("codec", "speaker", "text")
PROFILE_NAME = "selected-features-v1"
NATIVE_DURATION_TOLERANCE_SECONDS = 0.02


def output_schema(codec, speaker, text):
    fields = list(text)
    for prefix, schema in (("codec", codec), ("speaker", speaker)):
        fields.extend(
            pa.field(f"{prefix}_{f.name}", f.type, nullable=f.nullable)
            for f in schema
            if f.name not in {"target_id", "audio_sha256", *codec_text_schema().names}
        )
    return pa.schema(fields)


def require_equal(left, right, label):
    if not left.equals(right):
        raise ValueError(f"Feature mismatch: {label}")


def merge_tables(codec, speaker, text, dataset_id, release_id):
    """ID join, full metadata/range checks, zero numerical transformations."""
    reference = "reference_codec_start" in speaker.schema.names
    ids = text["target_id"]
    if ids.null_count or pc.count_distinct(ids).as_py() != len(ids):
        raise ValueError("Null or duplicate text targets")
    codec, speaker = align(codec, ids), align(speaker, ids)
    for column, value in (("dataset_id", dataset_id), ("release_id", release_id)):
        if text[column].null_count or not pc.all(pc.equal(text[column], value)).as_py():
            raise ValueError(f"Wrong {column}")
    for table in (codec, speaker):
        require_equal(table["audio_sha256"], text["audio_sha256"], "audio_sha256")
        if table["audio_sha256"].null_count:
            raise ValueError("Null audio identity")
        checks = [("status", "ok"), ("target_kind", "sample")]
        if table is codec or not reference:
            checks.append(("start_frame", 0))
        for column, value in checks:
            if table[column].null_count or not pc.all(pc.equal(table[column], value)).as_py():
                raise ValueError(f"Unsupported audio feature {column}")
        if table["error_code"].null_count != len(ids):
            raise ValueError("Successful feature has error_code")
        require_equal(table["parent_sample_id"], ids, "whole-sample parent identity")
        for column in ("end_frame", "native_sample_rate", "encoder_input_num_frames"):
            if table[column].null_count or not pc.all(pc.greater(table[column], 0)).as_py():
                raise ValueError(f"Invalid {column}")
    for column in codec_text_schema().names:
        require_equal(codec[column], text[column], f"selected {column}")
    require_equal(codec["native_sample_rate"], speaker["native_sample_rate"], "native rate")
    differences = []
    if reference:
        from .speaker.qwen3_ecapa.reference import validate

        metadata = ["target_id", "audio_sha256", "native_sample_rate", "start_frame", "end_frame"]
        for ref, full in zip(
            speaker.select(
                metadata
                + [
                    "reference_codec_start",
                    "reference_codec_end",
                    "reference_codec_feature_key",
                    "reference_native_total_frames",
                ]
            ).to_pylist(),
            codec.select(metadata + ["feature_key", "num_codec_frames"]).to_pylist(),
            strict=True,
        ):
            validate(ref, full)
    else:
        delta = pc.subtract(codec["end_frame"], speaker["end_frame"]).to_numpy()
        different = np.flatnonzero(delta)
        differences = [
            dict(
                target_id=ids[int(i)].as_py(),
                codec_end_frame=codec["end_frame"][int(i)].as_py(),
                speaker_end_frame=speaker["end_frame"][int(i)].as_py(),
            )
            for i in different
        ]
        tolerance = np.maximum(
            2, np.ceil(codec["native_sample_rate"].to_numpy() * NATIVE_DURATION_TOLERANCE_SECONDS)
        )
        if np.any(np.abs(delta) > tolerance):
            raise ValueError(f"Native ranges differ by more than 20 ms: {differences[:10]}")
    native = codec["end_frame"].to_numpy()
    rate = codec["native_sample_rate"].to_numpy()
    frames = codec["encoder_input_num_frames"].to_numpy()
    if not np.array_equal(frames, (native * 24000 + rate - 1) // rate):
        raise ValueError("Codec input length does not match actual range")
    if codec["codes"].null_count or speaker["embedding"].null_count:
        raise ValueError("Missing feature payload")
    expected = (frames + 1919) // 1920
    if not np.array_equal(expected, codec["num_codec_frames"].to_numpy()) or not np.array_equal(
        expected, pc.list_value_length(codec["codes"]).to_numpy()
    ):
        raise ValueError("Codec frame count differs from waveform length")
    # Torchaudio uses FP32 ceil; its frontend differs from codec's SciPy resampler.
    sn = pc.subtract(speaker["end_frame"], speaker["start_frame"]).to_numpy()
    sr = speaker["native_sample_rate"].to_numpy()
    divisor = np.gcd(sr, 24000)
    sp_expected = np.ceil(((24000 // divisor) * sn / (sr // divisor)).astype(np.float32)).astype(
        np.int64
    )
    sp_expected = np.where(sr == 24000, sn, sp_expected)
    if not np.array_equal(sp_expected, speaker["encoder_input_num_frames"].to_numpy()):
        raise ValueError("Speaker input length does not match actual range")
    values = pc.list_flatten(pc.list_flatten(codec["codes"]))
    bounds = pc.min_max(values).as_py()
    if values.null_count or bounds["min"] < 0 or bounds["max"] >= 2048:
        raise ValueError("Invalid codec values")
    embedding = pc.list_flatten(speaker["embedding"])
    if embedding.null_count or not pc.all(pc.is_finite(embedding)).as_py():
        raise ValueError("Nonfinite speaker embedding")
    for table, column, size in (
        (codec, "num_codebooks", codec.schema.field("codes").type.value_type.list_size),
        (speaker, "embedding_dim", speaker.schema.field("embedding").type.list_size),
    ):
        if table[column].null_count or not pc.all(pc.equal(table[column], size)).as_py():
            raise ValueError(f"Wrong {column}")
    schema = output_schema(codec.schema, speaker.schema, text.schema)
    arrays = list(text.columns)
    for table in (codec, speaker):
        arrays.extend(
            table[f.name]
            for f in table.schema
            if f.name not in {"target_id", "audio_sha256", *codec_text_schema().names}
        )
    return pa.Table.from_arrays(arrays, schema=schema), differences


def prepare(
    codec_plan, speaker_plan, text_plan, work, *, output_root=None, datasets=None, batch_rows=32768
):
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    paths = dict(codec=codec_plan, speaker=speaker_plan, text=text_plan)
    plans = {k: json.loads(Path(v).read_text()) for k, v in paths.items()}
    reference = "reference" in plans["speaker"].get("profile", {})
    codec = plans["codec"]
    codec_datasets = list(codec["datasets"])
    if "predecessor_completed_datasets" in codec:
        old = codec["predecessor_completed_datasets"]
        predecessor = Path(old["work"]) / "plan.json"
        if file_hash(predecessor) != old["plan_sha256"]:
            raise ValueError("Predecessor plan changed")
        codec_datasets.extend(
            d
            for d in json.loads(predecessor.read_text())["datasets"]
            if d["source"]["dataset_id"] in old["datasets"]
        )
    maps = {
        k: {d["source"]["dataset_id"]: d for d in ds}
        for k, ds in (
            ("codec", codec_datasets),
            ("speaker", plans["speaker"]["datasets"]),
            ("text", plans["text"]["datasets"]),
        )
    }
    chosen = set(datasets) if datasets else set(maps["text"])
    if not chosen or any(not chosen <= set(m) for m in maps.values()):
        raise ValueError("Missing source dataset")
    work = Path(work).resolve()
    created = datetime.now(BEIJING).isoformat()
    p = dict(
        work=str(work),
        root=plans["text"]["root"],
        output_root=str(Path(output_root or plans["text"]["root"]).resolve()),
        created_at=created,
        execution_code_sha256=file_hash(__file__),
        selection_path=plans["text"]["selection_path"],
        selection_sha256=plans["text"]["selection_sha256"],
        datasets=[],
    )
    for name in sorted(chosen):
        sources = {}
        for kind in KINDS:
            original = maps[kind][name]
            path = Path(original["final"]) / "manifest.json"
            m = json.loads(path.read_text())
            validate_feature_coverage(m)
            if m["status"] != "complete" or (
                m["coverage"]["ok"] != m["rows"] and not (reference and kind == "speaker")
            ):
                raise ValueError("Source features are incomplete")
            if (
                m["selection"]["manifest_sha256"] != p["selection_sha256"]
                or m["selection"]["target_set_sha256"] != maps["text"][name]["target_set_sha256"]
                or m["release_id"] != maps["text"][name]["source"]["release_id"]
            ):
                raise ValueError("Source selections or releases differ")
            item = dict(
                manifest_path=str(path),
                manifest_sha256=file_hash(path),
                table_path=str(path.parent / "features.lance"),
                lance_version=m["lance_version"],
                run_id=m["run_id"],
                profile_id=m["profile_id"],
            )
            if source_table(item).count_rows() != maps["text"][name]["target_count"]:
                raise ValueError("Source row count differs")
            sources[kind] = item
        original = maps["text"][name]
        output_count = json.loads(Path(sources["speaker"]["manifest_path"]).read_text())[
            "coverage"
        ]["ok"]
        if output_count == 0:
            raise ValueError("No successful speaker references in dataset")
        definition = dict(
            kind="merged",
            sources={k: v["profile_id"] for k, v in sources.items()},
            native_duration_tolerance_seconds=NATIVE_DURATION_TOLERANCE_SECONDS,
            payload_transform="none",
            speaker_mode="codec_grid_reference" if reference else "whole_sample",
            execution_code_sha256=p["execution_code_sha256"],
        )
        run_id = make_feature_run_id(
            name, "merged", "reference-features-v1" if reference else PROFILE_NAME, created
        )
        release = Path(p["output_root"]) / "datasets" / name / original["source"]["release_id"]
        final = release / "features/merged" / run_id
        tasks = []
        for offset in range(0, original["target_count"], batch_rows):
            t = dict(
                dataset=name, offset=offset, rows=min(batch_rows, original["target_count"] - offset)
            )
            t["id"] = digest(t)
            tasks.append(t)
        p["datasets"].append(
            dict(
                source=original["source"],
                sources=sources,
                target_count=original["target_count"],
                output_count=output_count,
                reference=reference,
                target_set_sha256=original["target_set_sha256"],
                target_tasks=original["tasks"],
                tasks=tasks,
                profile=definition,
                profile_id=digest(definition),
                run_id=run_id,
                final=str(final),
                stage=str(final) + ".incomplete",
                state=str(
                    release.parent
                    / ".state"
                    / original["source"]["release_id"]
                    / "features/merged"
                    / run_id
                ),
            )
        )
    work.mkdir(parents=True, exist_ok=False)
    write_json(work / "plan.json", p)
    event(work, "planned", rows_total=sum(d["target_count"] for d in p["datasets"]))
    return p


def validate_sources(p, d):
    if file_hash(p["selection_path"]) != p["selection_sha256"]:
        raise ValueError("Selection changed")
    for item in d["sources"].values():
        if file_hash(item["manifest_path"]) != item["manifest_sha256"]:
            raise ValueError("Source feature manifest changed")


def write_chunk(d, task, table, differences, excluded_reasons=None):
    destination = Path(d["stage"]) / "features.lance"
    fragments = (
        write_fragments(
            table,
            destination,
            mode="create",
            data_storage_version="2.2",
            max_bytes_per_file=1024**3,
        )
        if table.num_rows
        else []
    )
    files, back = [], []
    for fragment in fragments:
        for f in fragment.files:
            path = destination / "data" / f.path
            back.append(pa.Table.from_batches(LanceFileReader(str(path)).read_all().to_batches()))
            files.append(dict(name=path.name, bytes=path.stat().st_size, sha256=file_hash(path)))
    if back and not pa.concat_tables(back).equals(table):
        raise ValueError("Merged payload readback differs")
    c = dict(
        task=task,
        rows=table.num_rows,
        input_rows=task["rows"],
        profile_id=d["profile_id"],
        fragments=[f.to_json() for f in fragments],
        files=files,
        payloads_validated=True,
        ordered_ids_sha256=ordered_hash(table["target_id"].to_pylist()),
        native_frame_differences=differences,
        excluded_reasons=excluded_reasons or {},
    )
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", c)
    return c


def finalize(p, d, checkpoints, schema):
    validate_sources(p, d)
    destination = Path(d["stage"]) / "features.lance"
    fragments = [
        FragmentMetadata.from_json(json.dumps(f)) for c in checkpoints for f in c["fragments"]
    ]
    previous = lance.dataset(destination).version if (destination / "_versions").exists() else 0
    ds = lance.LanceDataset.commit(
        destination, lance.LanceOperation.Overwrite(schema, fragments), read_version=previous
    )
    ids = (
        sid
        for b in ds.scanner(columns=["target_id"], scan_in_order=True).to_batches()
        for sid in b["target_id"].to_pylist()
    )
    verification_tasks = checkpoints if d.get("reference") else d["target_tasks"]
    for task in verification_tasks:
        actual = [next(ids, None) for _ in range(task["rows"])]
        if None in actual or ordered_hash(actual) != task["ordered_ids_sha256"]:
            raise ValueError("Merged targets differ from pinned selection task")
    if next(ids, None) is not None or ds.count_rows() != d.get("output_count", d["target_count"]):
        raise ValueError("Merged coverage differs")
    ds.create_scalar_index("target_id", "BTREE")
    if d["run_id"] in ds.tags.list():
        ds.tags.delete(d["run_id"])
    ds.tags.create(d["run_id"], ds.version)
    text = json.loads(Path(d["sources"]["text"]["manifest_path"]).read_text())
    count = d.get("output_count", d["target_count"])
    selection = text["selection"]
    if d.get("reference"):
        target_schema = feature_targets_schema()

        def target_batches():
            for batch in ds.to_batches(columns=["target_id"], batch_size=65536):
                ids = batch["target_id"]
                yield pa.RecordBatch.from_arrays(
                    [pa.array(["sample"] * len(ids)), ids, ids], schema=target_schema
                )

        targets = lance.write_dataset(
            pa.RecordBatchReader.from_batches(target_schema, target_batches()),
            Path(d["stage"]) / "targets.lance",
            mode="overwrite",
        )
        targets.create_scalar_index("target_id", "BTREE")
        target_ids = np.asarray(
            targets.to_table(columns=["target_id"])["target_id"].to_pylist(), dtype="S64"
        )
        target_hash = sorted_set_hash(target_ids)
        if len(target_ids) != count:
            raise ValueError("Merged subset target coverage differs")
        selection = dict(
            selection,
            mode="subset",
            target_count=count,
            target_set_sha256=target_hash,
            table=dict(
                table_path=f"features/merged/{d['run_id']}/targets.lance",
                branch=None,
                lance_version=targets.version,
                rows=count,
                schema_sha256=digest(schema_description(target_schema)),
            ),
        )
        selection.pop("filter", None)
    manifest = dict(
        contract_version="v0.1",
        artifact_kind="feature",
        kind="merged",
        status="complete",
        dataset_id=d["source"]["dataset_id"],
        release_id=d["source"]["release_id"],
        run_id=d["run_id"],
        profile_name="reference-features-v1" if d.get("reference") else PROFILE_NAME,
        profile_id=d["profile_id"],
        profile=d["profile"],
        target_kind="sample",
        rows=count,
        storage_format="lance",
        storage_kind="result_table",
        storage_version="2.2",
        schema_version="v0.1",
        schema_sha256=digest(schema_description(schema)),
        table_path=f"features/merged/{d['run_id']}/features.lance",
        lance_version=ds.version,
        input_root=p["root"],
        inputs=[
            *text["inputs"],
            *[dict(alias=k, artifact_kind="feature", **v) for k, v in d["sources"].items()],
        ],
        selection=selection,
        coverage=dict(total_targets=count, ok=count, failed=0, unsupported=0, skipped=0, missing=0),
        validation=dict(
            full_fragment_readback=True,
            id_join_rows=count,
            audio_hashes_checked=count,
            native_ranges_checked=count,
            selected_text_checked=count,
            native_frame_difference_rows=sum(
                len(c["native_frame_differences"]) for c in checkpoints
            ),
            source_tables_preserved=True,
            target_tasks_checked=len(verification_tasks),
        ),
        execution=dict(code_sha256=p["execution_code_sha256"]),
        finished_at=datetime.now(BEIJING).isoformat(),
    )
    if d.get("reference"):
        errors = dict(sum((Counter(c.get("excluded_reasons", {})) for c in checkpoints), Counter()))
        if count + sum(errors.values()) != d["target_count"]:
            raise ValueError("Reference merge does not account for every input")
        manifest["reference_filtering"] = dict(
            input_rows=d["target_count"], retained=count, excluded_reasons=errors
        )
    validate_feature_coverage(manifest)
    write_json(Path(d["stage"]) / "manifest.json", manifest)
    Path(d["stage"]).rename(d["final"])
    return manifest


def merge_dataset(p, d, stop):
    name = d["source"]["dataset_id"]
    progress = Path(p["work"]) / "datasets" / f"{name}.json"
    validate_sources(p, d)
    if Path(d["final"]).exists():
        m = json.loads((Path(d["final"]) / "manifest.json").read_text())
        if (
            m["profile_id"] != d["profile_id"]
            or m["rows"] != d.get("output_count", d["target_count"])
            or lance.dataset(
                Path(d["final"]) / "features.lance", version=m["lance_version"]
            ).count_rows()
            != d.get("output_count", d["target_count"])
        ):
            raise ValueError("Published merged output differs")
        write_json(progress, dict(phase="complete", rows_done=d["target_count"]))
        return
    stage = Path(d["stage"])
    owner = digest(p)
    if stage.exists():
        if json.loads((stage / "execution.json").read_text())["owner"] != owner:
            raise ValueError("Output owned by another plan")
    else:
        stage.mkdir(parents=True)
        write_json(stage / "execution.json", dict(owner=owner))
    tables = {k: source_table(v) for k, v in d["sources"].items()}
    schema = output_schema(*(tables[k].schema for k in KINDS))
    checkpoints, done = [], 0
    for task in d["tasks"]:
        if stop.is_set():
            return
        c = verify_checkpoint(d, task)
        if c is None:
            text = (
                tables["text"]
                .scanner(
                    offset=task["offset"],
                    limit=task["rows"],
                    scan_in_order=True,
                    batch_readahead=1,
                    fragment_readahead=1,
                )
                .to_table()
            )
            audio = [read_aligned(tables[k], task, text["target_id"]) for k in ("codec", "speaker")]
            for k, t in zip(("codec", "speaker"), audio, strict=True):
                if (
                    t["profile_id"].null_count
                    or not pc.all(pc.equal(t["profile_id"], d["sources"][k]["profile_id"])).as_py()
                ):
                    raise ValueError("Unexpected source profile")
            excluded = Counter()
            if d.get("reference"):
                audio = [align(t, text["target_id"]) for t in audio]
                require_equal(audio[1]["audio_sha256"], text["audio_sha256"], "reference source")
                status = audio[1]["status"].to_pylist()
                errors = audio[1]["error_code"].to_pylist()
                for value, error in zip(status, errors, strict=True):
                    if value not in {"ok", "failed"} or (value == "failed" and not error):
                        raise ValueError("Invalid reference status")
                    if value == "failed":
                        excluded[error] += 1
                mask = pc.equal(audio[1]["status"], "ok")
                audio = [t.filter(mask) for t in audio]
                text = text.filter(mask)
            if text.num_rows:
                merged, differences = merge_tables(*audio, text, name, d["source"]["release_id"])
            else:
                merged, differences = pa.Table.from_batches([], schema=schema), []
            c = write_chunk(d, task, merged, differences, dict(excluded))
        if c["profile_id"] != d["profile_id"]:
            raise ValueError("Checkpoint profile changed")
        checkpoints.append(c)
        done += c.get("input_rows", c["rows"])
        write_json(progress, dict(phase="merging", rows_done=done, rows_total=d["target_count"]))
    write_json(progress, dict(phase="publishing", rows_done=done, rows_total=d["target_count"]))
    finalize(p, d, checkpoints, schema)
    write_json(progress, dict(phase="complete", rows_done=done, rows_total=d["target_count"]))


def run(work, workers=2):
    work = Path(work)
    p = json.loads((work / "plan.json").read_text())
    if workers < 1 or file_hash(__file__) != p["execution_code_sha256"]:
        raise ValueError("Invalid worker count or execution code changed")
    total = sum(d["target_count"] for d in p["datasets"])
    stop = threading.Event()
    started = time.monotonic()
    with (work / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(merge_dataset, p, d, stop) for d in p["datasets"]]
            pending = set(futures)
            try:
                while pending:
                    done, pending = wait(pending, timeout=20, return_when="FIRST_COMPLETED")
                    for future in done:
                        future.result()
                    states = [json.loads(f.read_text()) for f in (work / "datasets").glob("*.json")]
                    event(
                        work,
                        "merging",
                        rows_done=sum(s["rows_done"] for s in states),
                        rows_total=total,
                        published=sum(s["phase"] == "complete" for s in states),
                        elapsed_seconds=time.monotonic() - started,
                    )
            except BaseException as exc:
                stop.set()
                for future in pending:
                    future.cancel()
                event(work, "failed", error_type=type(exc).__name__, error=str(exc))
                raise
    event(
        work,
        "complete",
        rows=total,
        datasets=len(p["datasets"]),
        elapsed_seconds=time.monotonic() - started,
    )
