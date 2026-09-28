"""Read all source IDs and compare LibriHeavy configs without loading audio bytes."""

import itertools
import json
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path("/workspace/data/DATA-TTS/libriheavy")
OUT = Path(__file__).resolve().parents[1] / "reports/source-check"


def read_ids(path):
    parquet = pq.ParquetFile(path)
    ids = parquet.read(columns=["id"], use_threads=False).column("id").to_pylist()
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError(f"Invalid ID: {path}")
    return ids


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    configs = sorted(p.name for p in (ROOT / "default").iterdir() if p.is_dir())
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "all local Parquet IDs, exact string equality; no waveform deduplication",
        "configs": {},
        "intersections": [],
    }
    with tempfile.TemporaryDirectory(prefix="libriheavy-ids-", dir=OUT) as work:
        db = sqlite3.connect(str(Path(work) / "ids.sqlite"))
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute("PRAGMA cache_size=-262144")
        db.execute("CREATE TABLE ids(id TEXT NOT NULL, config INTEGER NOT NULL)")
        for index, config in enumerate(configs):
            files = sorted((ROOT / "default" / config).rglob("*.parquet"))
            count = 0
            # Bound pending reads so audio-scale corpora cannot fill memory with queued IDs.
            with ThreadPoolExecutor(max_workers=16) as pool:
                for start in range(0, len(files), 32):
                    for ids in pool.map(read_ids, files[start : start + 32]):
                        db.executemany("INSERT INTO ids VALUES (?,?)", ((v, index) for v in ids))
                        count += len(ids)
            db.commit()
            report["configs"][config] = {"files": len(files), "rows": count}
            print(config, count, flush=True)
        print("Building exact ID index", flush=True)
        db.execute("CREATE INDEX by_id ON ids(id, config)")
        db.commit()
        for i, config in enumerate(configs):
            unique = db.execute(
                "SELECT COUNT(DISTINCT id) FROM ids WHERE config=?", (i,)
            ).fetchone()[0]
            report["configs"][config]["unique_ids"] = unique
            report["configs"][config]["duplicate_rows_within_config"] = (
                report["configs"][config]["rows"] - unique
            )
        for i, j in itertools.combinations(range(len(configs)), 2):
            count = db.execute(
                "SELECT COUNT(DISTINCT a.id) FROM ids a JOIN ids b ON a.id=b.id "
                "WHERE a.config=? AND b.config=?",
                (i, j),
            ).fetchone()[0]
            report["intersections"].append(
                {"left": configs[i], "right": configs[j], "shared_ids": count}
            )
            if count:
                print("overlap", configs[i], configs[j], count, flush=True)
        report["total_rows"] = db.execute("SELECT COUNT(*) FROM ids").fetchone()[0]
        report["unique_ids"] = db.execute("SELECT COUNT(DISTINCT id) FROM ids").fetchone()[0]
        report["overlap_examples"] = db.execute(
            "SELECT id,group_concat(DISTINCT config),COUNT(*) FROM ids "
            "GROUP BY id HAVING COUNT(*)>1 LIMIT 10"
        ).fetchall()
        db.close()
    (OUT / "libriheavy-overlap.json").write_text(json.dumps(report, indent=2) + "\n")
    print("DONE", report["total_rows"], report["unique_ids"], flush=True)


if __name__ == "__main__":
    main()
