"""Join published feature snapshots by sample ID, preserving all three source tables."""

import fcntl
import json
import multiprocessing
import os
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
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
AUDIO_KINDS = ("codec", "speaker")
EXCLUSION_RULE = (
    "Targets whose codec or speaker row has status != ok are excluded from the merged table "
    "and counted per source and error code; text sources must be complete."
)
VERIFY_THREADS = min(32, os.cpu_count() or 1)


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
        original = maps["text"][name]
        target_count = original["target_count"]
        sources, failures, excluded_ids = {}, {}, set()
        for kind in KINDS:
            path = Path(maps[kind][name]["final"]) / "manifest.json"
            m = json.loads(path.read_text())
            validate_feature_coverage(m)
            # Audio features may carry failed rows (excluded below); text must be complete.
            if m["status"] != "complete" or (kind == "text" and m["coverage"]["ok"] != m["rows"]):
                raise ValueError("Source features are incomplete")
            if (
                m["selection"]["manifest_sha256"] != p["selection_sha256"]
                or m["selection"]["target_set_sha256"] != original["target_set_sha256"]
                or m["release_id"] != original["source"]["release_id"]
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
            table = source_table(item)
            if table.count_rows() != target_count:
                raise ValueError("Source row count differs")
            sources[kind] = item
            if kind in AUDIO_KINDS:
                failures[kind] = m["rows"] - m["coverage"]["ok"]
                if failures[kind]:
                    failed = table.to_table(
                        columns=["target_id"], filter="status IS NULL OR status != 'ok'"
                    )["target_id"].to_pylist()
                    if len(failed) != failures[kind]:
                        raise ValueError("Source coverage differs from table statuses")
                    excluded_ids.update(failed)
        output_count = target_count - len(excluded_ids)
        if output_count == 0:
            raise ValueError("No successful targets in dataset")
        definition = dict(
            kind="merged",
            sources={k: v["profile_id"] for k, v in sources.items()},
            native_duration_tolerance_seconds=NATIVE_DURATION_TOLERANCE_SECONDS,
            payload_transform="none",
            speaker_mode="codec_grid_reference" if reference else "whole_sample",
            failed_audio_rows="excluded",
            execution_code_sha256=p["execution_code_sha256"],
        )
        run_id = make_feature_run_id(
            name, "merged", "reference-features-v1" if reference else PROFILE_NAME, created
        )
        release = Path(p["output_root"]) / "datasets" / name / original["source"]["release_id"]
        final = release / "features/merged" / run_id
        tasks = []
        for offset in range(0, target_count, batch_rows):
            t = dict(dataset=name, offset=offset, rows=min(batch_rows, target_count - offset))
            t["id"] = digest(t)
            tasks.append(t)
        p["datasets"].append(
            dict(
                source=original["source"],
                sources=sources,
                target_count=target_count,
                output_count=output_count,
                excluded_count=len(excluded_ids),
                source_failures=failures,
                reference=reference,
                target_set_sha256=original["target_set_sha256"],
                target_tasks=original["tasks"],
                tasks=tasks,
                profile=definition,
                profile_id=digest(definition),
                run_id=run_id,
                final=str(final),
                stage=str(final) + ".incomplete",
                # Work state stays outside the unified root.
                state=str(work / "state" / name),
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


def write_chunk(d, task, table, differences, excluded_reasons=None, excluded_rows=0):
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
        # Per source kind: error_code -> rows. A target failing in both sources is
        # counted once in excluded_rows but once per source here.
        excluded_reasons=excluded_reasons or {},
        excluded_rows=excluded_rows,
    )
    write_json(Path(d["state"]) / "checkpoints" / f"{task['id']}.json", c)
    return c


def merge_chunk(p, d, task, tables, schema):
    """Merge one pinned row range, drop failed audio rows, and checkpoint the result."""
    name = d["source"]["dataset_id"]
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
    ids = text["target_id"]
    if text.num_rows != task["rows"]:
        raise ValueError("Text source rows differ from pinned task")
    audio = {k: align(read_aligned(tables[k], task, ids), ids) for k in AUDIO_KINDS}
    keep = np.ones(text.num_rows, dtype=bool)
    excluded = {}
    for kind, t in audio.items():
        if (
            t["profile_id"].null_count
            or not pc.all(pc.equal(t["profile_id"], d["sources"][kind]["profile_id"])).as_py()
        ):
            raise ValueError("Unexpected source profile")
        # Identity must hold for failed rows too: they are the same selected targets.
        require_equal(t["audio_sha256"], text["audio_sha256"], f"{kind} source identity")
        reasons = Counter()
        all_ok = pc.all(pc.equal(t["status"], "ok")).as_py()
        if not (all_ok and t["error_code"].null_count == len(t)):
            statuses = t["status"].to_pylist()
            errors = t["error_code"].to_pylist()
            for i, (status, error) in enumerate(zip(statuses, errors, strict=True)):
                if status == "ok":
                    continue
                if status != "failed" or not error:
                    raise ValueError(f"Invalid {kind} status")
                reasons[error] += 1
                keep[i] = False
        excluded[kind] = dict(reasons)
    if not keep.all():
        mask = pa.array(keep)
        audio = {k: t.filter(mask) for k, t in audio.items()}
        text = text.filter(mask)
    if text.num_rows:
        merged, differences = merge_tables(
            audio["codec"], audio["speaker"], text, name, d["source"]["release_id"]
        )
    else:
        merged, differences = pa.Table.from_batches([], schema=schema), []
    return write_chunk(d, task, merged, differences, excluded, int(np.count_nonzero(~keep)))


def finalize(p, d, checkpoints, schema):
    validate_sources(p, d)
    if [c["task"] for c in checkpoints] != d["tasks"]:
        raise ValueError("Checkpoints do not cover the planned tasks in order")
    destination = Path(d["stage"]) / "features.lance"
    fragments = [
        FragmentMetadata.from_json(json.dumps(f)) for c in checkpoints for f in c["fragments"]
    ]
    previous = lance.dataset(destination).version if (destination / "_versions").exists() else 0
    ds = lance.LanceDataset.commit(
        destination, lance.LanceOperation.Overwrite(schema, fragments), read_version=previous
    )
    count = d["output_count"]
    excluded_rows = sum(c["excluded_rows"] for c in checkpoints)
    by_source = {
        kind: dict(sum((Counter(c["excluded_reasons"].get(kind)) for c in checkpoints), Counter()))
        for kind in AUDIO_KINDS
    }
    if (
        sum(c["rows"] for c in checkpoints) != count
        or count + excluded_rows != d["target_count"]
        or excluded_rows != d["excluded_count"]
        or any(sum(by_source[k].values()) != d["source_failures"][k] for k in AUDIO_KINDS)
    ):
        raise ValueError("Merged output does not account for every input target")
    subset = d["reference"] or excluded_rows > 0
    ids = (
        sid
        for b in ds.scanner(columns=["target_id"], scan_in_order=True).to_batches()
        for sid in b["target_id"].to_pylist()
    )
    verification_tasks = checkpoints if subset else d["target_tasks"]
    for task in verification_tasks:
        actual = [next(ids, None) for _ in range(task["rows"])]
        if None in actual or ordered_hash(actual) != task["ordered_ids_sha256"]:
            raise ValueError("Merged targets differ from pinned selection task")
    if next(ids, None) is not None or ds.count_rows() != count:
        raise ValueError("Merged coverage differs")
    ds.create_scalar_index("target_id", "BTREE")
    if d["run_id"] in ds.tags.list():
        ds.tags.delete(d["run_id"])
    ds.tags.create(d["run_id"], ds.version)
    text = json.loads(Path(d["sources"]["text"]["manifest_path"]).read_text())
    selection = text["selection"]
    if subset:
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
        profile_name="reference-features-v1" if d["reference"] else PROFILE_NAME,
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
        excluded_targets=dict(
            rule=EXCLUSION_RULE,
            input_rows=d["target_count"],
            retained=count,
            total=excluded_rows,
            by_source=by_source,
        ),
        validation=dict(
            full_fragment_readback=True,
            id_join_rows=count,
            audio_hashes_checked=d["target_count"],
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
    if d["reference"]:
        manifest["reference_filtering"] = dict(
            input_rows=d["target_count"], retained=count, excluded_reasons=by_source["speaker"]
        )
    validate_feature_coverage(manifest)
    write_json(Path(d["stage"]) / "manifest.json", manifest)
    # The ownership marker only guards the unpublished stage; it is not part of the release.
    (Path(d["stage"]) / "execution.json").unlink(missing_ok=True)
    Path(d["stage"]).rename(d["final"])
    return manifest


def published(d):
    """True when the dataset's final output exists and matches the plan."""
    final = Path(d["final"])
    if not final.exists():
        return False
    m = json.loads((final / "manifest.json").read_text())
    if (
        m["profile_id"] != d["profile_id"]
        or m["rows"] != d["output_count"]
        or lance.dataset(final / "features.lance", version=m["lance_version"]).count_rows()
        != d["output_count"]
    ):
        raise ValueError("Published merged output differs")
    return True


def claim_stage(p, d):
    stage = Path(d["stage"])
    owner = digest(p)
    marker = stage / "execution.json"
    if marker.exists():
        if json.loads(marker.read_text())["owner"] != owner:
            raise ValueError("Output owned by another plan")
    elif stage.exists() and any(stage.iterdir()):
        raise ValueError("Output owned by another plan")
    else:
        stage.mkdir(parents=True, exist_ok=True)
        write_json(marker, dict(owner=owner))


def verified_checkpoints(jobs, threads=VERIFY_THREADS):
    """Verify checkpoints for (dataset, task) pairs in parallel; None marks missing ones."""
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(threads, len(jobs))) as pool:
        results = list(pool.map(lambda job: verify_checkpoint(*job), jobs))
    for (d, _), c in zip(jobs, results, strict=True):
        if c is not None and c["profile_id"] != d["profile_id"]:
            raise ValueError("Checkpoint profile changed")
    return results


_WORKER = {}


def worker_plan(plan_path):
    """Per-process cache of the plan and opened source tables."""
    plan_path = str(plan_path)
    if _WORKER.get("path") != plan_path:
        _WORKER.update(path=plan_path, plan=json.loads(Path(plan_path).read_text()), datasets={})
    return _WORKER


def run_task(plan_path, index, task):
    """Process-pool entry: merge one chunk and return a slim summary of its checkpoint."""
    w = worker_plan(plan_path)
    p = w["plan"]
    d = p["datasets"][index]
    opened = w["datasets"].get(index)
    if opened is None:
        tables = {k: source_table(v) for k, v in d["sources"].items()}
        opened = w["datasets"][index] = (tables, output_schema(*(tables[k].schema for k in KINDS)))
    c = merge_chunk(p, d, task, *opened)
    return {k: c[k] for k in ("task", "rows", "input_rows", "profile_id", "excluded_rows")}


def finalize_dataset(plan_path, index):
    """Process-pool entry: re-verify every checkpoint from disk, then publish the dataset."""
    p = json.loads(Path(plan_path).read_text())
    d = p["datasets"][index]
    checkpoints = verified_checkpoints([(d, task) for task in d["tasks"]])
    if any(c is None for c in checkpoints):
        raise ValueError("Checkpoint missing before publication")
    tables = {k: source_table(v) for k, v in d["sources"].items()}
    schema = output_schema(*(tables[k].schema for k in KINDS))
    manifest = finalize(p, d, checkpoints, schema)
    return dict(rows=manifest["rows"], excluded=manifest["excluded_targets"]["total"])


def progress(work, name, phase, rows_done, rows_total):
    write_json(
        Path(work) / "datasets" / f"{name}.json",
        dict(phase=phase, rows_done=rows_done, rows_total=rows_total),
    )


def execute(work, p, workers, started):
    """Dispatch chunk tasks of all datasets to one process pool; publish as each completes."""
    plan_path = str(work / "plan.json")
    total = sum(d["target_count"] for d in p["datasets"])
    names = {i: d["source"]["dataset_id"] for i, d in enumerate(p["datasets"])}
    index = {name: i for i, name in names.items()}
    rows_done, remaining, complete = {}, {}, set()
    verify = []
    for i, d in enumerate(p["datasets"]):
        validate_sources(p, d)
        rows_done[names[i]] = 0
        if published(d):
            complete.add(names[i])
            rows_done[names[i]] = d["target_count"]
            progress(work, names[i], "complete", d["target_count"], d["target_count"])
            continue
        claim_stage(p, d)
        remaining[names[i]] = 0
        verify.extend((d, task) for task in d["tasks"])
    jobs = []
    for (d, task), c in zip(verify, verified_checkpoints(verify), strict=True):
        name = d["source"]["dataset_id"]
        if c is None:
            remaining[name] += 1
            jobs.append((index[name], task))
        else:
            rows_done[name] += c["input_rows"]
    # Datasets closest to completion go first so publication overlaps chunk work.
    jobs.sort(key=lambda job: (remaining[names[job[0]]], job[0], job[1]["offset"]))
    for name in remaining:
        progress(work, name, "merging", rows_done[name], p["datasets"][index[name]]["target_count"])

    def report():
        event(
            work,
            "merging",
            rows_done=sum(rows_done.values()),
            rows_total=total,
            published=len(complete),
            datasets=len(p["datasets"]),
            in_flight=len(in_flight),
            publishing=len(publishing),
            elapsed_seconds=time.monotonic() - started,
        )

    ctx = multiprocessing.get_context("spawn")
    in_flight, publishing = {}, {}
    # Publishing is long, memory-heavy Python: keep it out of the coordinator and the
    # chunk pool so dispatch never stalls; at most two datasets publish at once.
    with (
        ProcessPoolExecutor(max_workers=2, mp_context=ctx) as publisher,
        ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool,
    ):

        def publish(name):
            d = p["datasets"][index[name]]
            progress(work, name, "publishing", rows_done[name], d["target_count"])
            publishing[publisher.submit(finalize_dataset, plan_path, index[name])] = name

        iterator = iter(jobs)

        def fill():
            # Bounded submission: never queue the whole plan as futures.
            while len(in_flight) < workers * 2:
                job = next(iterator, None)
                if job is None:
                    return
                in_flight[pool.submit(run_task, plan_path, *job)] = job

        try:
            for name in [n for n, left in remaining.items() if left == 0]:
                publish(name)
            fill()
            report()
            while in_flight or publishing:
                done, _ = wait([*in_flight, *publishing], timeout=20, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in in_flight:
                        i, task = in_flight.pop(future)
                        c = future.result()
                        d, name = p["datasets"][i], names[i]
                        if c["task"] != task or c["profile_id"] != d["profile_id"]:
                            raise ValueError("Worker returned a foreign checkpoint")
                        rows_done[name] += c["input_rows"]
                        remaining[name] -= 1
                        # Publish only once every dispatched chunk of the dataset has returned.
                        if remaining[name] == 0:
                            publish(name)
                        else:
                            progress(work, name, "merging", rows_done[name], d["target_count"])
                    else:
                        name = publishing.pop(future)
                        result = future.result()
                        complete.add(name)
                        d = p["datasets"][index[name]]
                        progress(work, name, "complete", rows_done[name], d["target_count"])
                        event(work, "dataset_published", dataset=name, **result)
                fill()
                report()
        except BaseException:
            for future in [*in_flight, *publishing]:
                future.cancel()
            raise
    if len(complete) != len(p["datasets"]):
        raise ValueError("Not every dataset was published")


def run(work, workers=16):
    work = Path(work)
    p = json.loads((work / "plan.json").read_text())
    if workers < 1 or file_hash(__file__) != p["execution_code_sha256"]:
        raise ValueError("Invalid worker count or execution code changed")
    started = time.monotonic()
    with (work / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            execute(work, p, workers, started)
        except BaseException as exc:
            event(work, "failed", error_type=type(exc).__name__, error=str(exc))
            raise
    event(
        work,
        "complete",
        rows=sum(d["target_count"] for d in p["datasets"]),
        datasets=len(p["datasets"]),
        elapsed_seconds=time.monotonic() - started,
    )
