"""Global short-ID deduplication, independent of worker and archive completion order."""

import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ...convert import file_hash
from ...feature_runtime import write_json
from ...schema import digest


def deduplicate(work):
    work = Path(work)
    plan = json.loads((work / "plan.json").read_text())
    completed = work / "dedup.json"
    if completed.exists():
        result = json.loads(completed.read_text())
        if result["plan_sha256"] != file_hash(work / "plan.json"):
            raise ValueError("Dedup plan changed")
        for item in result["partitions"]:
            if file_hash(work / item["path"]) != item["sha256"]:
                raise ValueError("Winner partition changed")
        return result
    # Rebuildable scratch is local; winners and candidate evidence remain in work.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="emilia2-dedup-") as tmp:
        db = sqlite3.connect(str(Path(tmp) / "ids.sqlite"))
        try:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA cache_size=-262144")
            db.execute(
                "CREATE TABLE ids(id TEXT PRIMARY KEY, semantic TEXT, conflict INTEGER, "
                "n INTEGER, archive INTEGER, row_number INTEGER, priority INTEGER, "
                "error TEXT) WITHOUT ROWID"
            )
            sql = """INSERT INTO ids VALUES(?,?,0,1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                conflict=MAX(ids.conflict,ids.semantic != excluded.semantic),n=ids.n+1,
                archive=CASE WHEN (ids.error IS NOT NULL AND excluded.error IS NULL)
                    OR ((ids.error IS NULL)=(excluded.error IS NULL)
                        AND excluded.priority<ids.priority)
                    THEN excluded.archive ELSE ids.archive END,
                row_number=CASE WHEN (ids.error IS NOT NULL AND excluded.error IS NULL)
                    OR ((ids.error IS NULL)=(excluded.error IS NULL)
                        AND excluded.priority<ids.priority)
                    THEN excluded.row_number ELSE ids.row_number END,
                priority=CASE WHEN (ids.error IS NOT NULL AND excluded.error IS NULL)
                    OR ((ids.error IS NULL)=(excluded.error IS NULL)
                        AND excluded.priority<ids.priority)
                    THEN excluded.priority ELSE ids.priority END,
                error=CASE WHEN excluded.error IS NULL THEN NULL ELSE ids.error END"""
            inputs = []
            for i, source in enumerate(plan["sources"]):
                checkpoint = work / "metadata" / f"{i:05d}.json"
                rep = json.loads(checkpoint.read_text())
                path = checkpoint.with_suffix(".parquet")
                if rep["source"] != source or rep["sha256"] != file_hash(path):
                    raise ValueError(f"Unverified candidate partition: {path}")
                inputs.append(dict(path=str(path.relative_to(work)), sha256=rep["sha256"]))
                pos = 0
                for batch in pq.ParquetFile(path).iter_batches(
                    batch_size=8192, columns=["short_id", "semantic_hash", "priority", "error"]
                ):
                    rows = batch.to_pylist()
                    db.executemany(
                        sql,
                        [
                            (
                                r["short_id"],
                                r["semantic_hash"],
                                i,
                                pos + j,
                                r["priority"],
                                r["error"],
                            )
                            for j, r in enumerate(rows)
                        ],
                    )
                    pos += len(rows)
                db.commit()
                if i % 100 == 0:
                    print(
                        json.dumps(dict(phase="dedup", archives=i + 1, total=len(plan["sources"]))),
                        flush=True,
                    )
            total, unique, duplicates, redundant, conflicts, invalid, selected = db.execute(
                "SELECT SUM(n),COUNT(*),SUM(n>1),SUM(n-1),SUM(conflict),"
                "SUM(conflict=0 AND error IS NOT NULL),"
                "SUM(conflict=0 AND error IS NULL) FROM ids"
            ).fetchone()
            db.execute("CREATE INDEX by_archive ON ids(archive,row_number)")
            partitions = []
            schema = pa.schema(
                [
                    ("row_number", pa.int64()),
                    ("short_id", pa.string()),
                    ("candidate_count", pa.int32()),
                ]
            )
            (work / "winners").mkdir(exist_ok=True)
            for i in range(len(plan["sources"])):
                rows = db.execute(
                    "SELECT row_number,id,n FROM ids WHERE archive=? "
                    "AND conflict=0 AND error IS NULL ORDER BY row_number",
                    (i,),
                )
                target = work / "winners" / f"{i:05d}.parquet"
                n = 0
                with pq.ParquetWriter(target, schema, compression="zstd") as writer:
                    while values := rows.fetchmany(8192):
                        writer.write_table(
                            pa.Table.from_pylist(
                                [dict(zip(schema.names, v, strict=True)) for v in values],
                                schema=schema,
                            )
                        )
                        n += len(values)
                partitions.append(
                    dict(path=str(target.relative_to(work)), rows=n, sha256=file_hash(target))
                )
            exclusions = work / "candidate-exclusions.jsonl"
            with exclusions.open("w") as f:
                for sid, conflict, error, n in db.execute(
                    "SELECT id,conflict,error,n FROM ids WHERE conflict=1 OR error IS NOT NULL"
                ):
                    f.write(
                        json.dumps(
                            dict(
                                short_id=sid,
                                reason="identity_conflict" if conflict else error,
                                candidates=n,
                            )
                        )
                        + "\n"
                    )
        finally:
            db.close()
    if sum(p["rows"] for p in partitions) != selected or unique != selected + conflicts + invalid:
        raise ValueError("Dedup count conservation failed")
    result = dict(
        plan_sha256=file_hash(work / "plan.json"),
        candidates=total,
        unique_ids=unique,
        duplicate_groups=duplicates,
        redundant_candidates=redundant,
        conflicting_ids=conflicts,
        invalid_ids=invalid,
        selected=selected,
        candidate_partitions_sha256=digest(inputs),
        partitions=partitions,
        exclusions_sha256=file_hash(exclusions),
    )
    write_json(completed, result)
    return result
