"""Emilia2 source planning, metadata scan and base publication commands."""

import argparse
import fcntl
import json
import multiprocessing
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from ...convert import file_hash
from ...feature_runtime import write_json
from .metadata import inventory, scan_archive
from .planning import deduplicate


def main():
    parser = argparse.ArgumentParser(__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    for name in ["scan", "dedup"]:
        p = sub.add_parser(name)
        p.add_argument("--work", type=Path, required=True)
        if name == "scan":
            p.add_argument("--workers", type=int, default=8)
    p = sub.add_parser("run")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    work = args.work.resolve()
    if args.command == "plan":
        if work.exists():
            raise FileExistsError(work)
        sources = inventory(args.root)
        work.mkdir(parents=True)
        write_json(
            work / "plan.json",
            dict(
                root=str(args.root.resolve()),
                sources=sources,
                metadata_code_sha256=file_hash(Path(__file__).with_name("metadata.py")),
                scope=(
                    "all listed Emilia2 archives; standalone short and "
                    "nested short from long/dialogue"
                ),
            ),
        )
        print(json.dumps(dict(archives=len(sources), work=str(work))), flush=True)
        return
    with (work / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "run":
            run_publication(work, args.output, args.workers)
            return
        if args.command == "dedup":
            print(json.dumps(deduplicate(work)), flush=True)
            return
        if args.workers < 1:
            raise ValueError("workers must be positive")
        p = json.loads((work / "plan.json").read_text())
        if p["metadata_code_sha256"] != file_hash(Path(__file__).with_name("metadata.py")):
            raise ValueError("Pinned metadata implementation changed")
        count = Counter()
        done = 0
        with ProcessPoolExecutor(
            max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            jobs = [
                pool.submit(scan_archive, (p["root"], str(work), i, s))
                for i, s in enumerate(p["sources"])
            ]
            for job in as_completed(jobs):
                try:
                    result = job.result()
                except BaseException:
                    for pending in jobs:
                        pending.cancel()
                    raise
                count.update(result["counts"])
                done += 1
                status = dict(
                    phase="metadata_scan",
                    archives_done=done,
                    archives_total=len(jobs),
                    counts=dict(count),
                )
                write_json(work / "status.json", status)
                if done % 20 == 0:
                    print(json.dumps(status), flush=True)
        write_json(work / "status.json", dict(status, phase="metadata_complete"))


def run_publication(work, output, workers):
    import shutil

    from ...schema import digest
    from .publication import convert_archive, finalize, ingestion_profile

    if workers < 1:
        raise ValueError("workers must be positive")
    output = output.resolve()
    stage = output.with_name(output.name + ".incomplete")
    if output.exists():
        raise FileExistsError(output)
    p = json.loads((work / "plan.json").read_text())
    raw_root = Path(p["root"]).resolve()
    if output.is_relative_to(raw_root) or raw_root.is_relative_to(output):
        raise ValueError("Output must be separate from raw sources")
    d = deduplicate(work)
    if not d["selected"]:
        raise ValueError("No valid short candidates")
    profile = ingestion_profile()
    record = dict(
        output=str(output),
        stage=str(stage),
        release_id=output.name,
        profile=profile,
        profile_id=digest(profile),
        plan_sha256=file_hash(work / "plan.json"),
        dedup_sha256=file_hash(work / "dedup.json"),
    )
    path = work / "publication.json"
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise ValueError("Publication configuration changed")
    else:
        if stage.exists():
            raise FileExistsError("Unowned stage exists")
        stage.mkdir(parents=True)
        write_json(path, record)
    (stage / "samples.lance/data").mkdir(parents=True, exist_ok=True)
    rows = failed = done = 0
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [pool.submit(convert_archive, (str(work), i)) for i in range(len(p["sources"]))]
        for future in as_completed(futures):
            try:
                r = future.result()
                if shutil.disk_usage(stage).free < 100 * 1024**3:
                    raise RuntimeError("Less than 100 GiB free; stopped before filling storage")
            except BaseException:
                for pending in futures:
                    pending.cancel()
                raise
            rows += r["rows"]
            failed += len(r["rejected"])
            done += 1
            status = dict(
                phase="converting",
                archives_done=done,
                archives_total=len(futures),
                rows=rows,
                audio_failed=failed,
                selected_total=d["selected"],
            )
            write_json(work / "status.json", status)
            print(json.dumps(status), flush=True)
    write_json(work / "status.json", dict(phase="finalizing", rows=rows))
    result = finalize(work)
    write_json(
        work / "status.json", dict(phase="complete", rows=result["rows"], output=str(output))
    )
