"""Disk-backed pairing: archive order never requires all audio in RAM."""

import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive member: {name}")
    return path.as_posix().removeprefix("./")


def inputs(root, pattern, config):
    if config not in (None, "all", "default"):
        raise ValueError("Supported config: all")
    files = sorted(root.glob(pattern))
    if not files:
        raise ValueError(f"No {pattern} source archives")
    return files


def lines(archive, member):
    with archive.extractfile(member) as handle:
        for index, line in enumerate(handle):
            yield line.decode("utf-8-sig" if index == 0 else "utf-8")


@contextmanager
def pairs():
    # Two temporary files per worker, not one file per utterance. Removed on close.
    with tempfile.TemporaryDirectory(prefix="tts-archive-") as folder:
        db = sqlite3.connect(str(Path(folder) / "pairs.sqlite"), check_same_thread=False)
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.execute(
            "CREATE TABLE pairs (key TEXT PRIMARY KEY, metadata TEXT, pos INTEGER, size INTEGER, "
            "done INTEGER DEFAULT 0)"
        )
        with (Path(folder) / "audio.bin").open("w+b") as audio:
            try:
                yield PairStore(db, audio)
            finally:
                db.close()


class PairStore:
    def __init__(self, db, audio):
        self.db, self.audio = db, audio
        self.count = 0
        self.lock = threading.RLock()

    def put(self, key, *, metadata=None, data=None):
        # Arrow may resume one input stream on a different Lance worker thread.
        # Serialize DB and spool access together; changing thread affinity is safe.
        with self.lock:
            return self._put(key, metadata=metadata, data=data)

    def _put(self, key, *, metadata=None, data=None):
        row = self.db.execute(
            "SELECT metadata,pos,size,done FROM pairs WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            self.db.execute("INSERT INTO pairs(key) VALUES (?)", (key,))
            row = (None, None, None, 0)
        meta, pos, size, done = row
        if (
            done
            or (metadata is not None and meta is not None)
            or (data is not None and pos is not None)
        ):
            raise ValueError(f"Duplicate archive pairing key: {key}")
        if metadata is not None:
            meta = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        if data is not None and meta is None:
            self.audio.seek(0, 2)
            pos, size = self.audio.tell(), len(data)
            self.audio.write(data)
        if meta is not None and (data is not None or pos is not None):
            if data is None:
                self.audio.seek(pos)
                data = self.audio.read(size)
                if len(data) != size:
                    raise ValueError("Incomplete temporary audio spool")
            self.db.execute(
                "UPDATE pairs SET done=1,metadata=NULL,pos=NULL,size=NULL WHERE key=?", (key,)
            )
            self.count += 1
            return data, json.loads(meta)
        self.db.execute(
            "UPDATE pairs SET metadata=?,pos=?,size=? WHERE key=?", (meta, pos, size, key)
        )
        return None

    def finish(self):
        with self.lock:
            self._finish()

    def _finish(self):
        missing = self.db.execute("SELECT key FROM pairs WHERE done=0 LIMIT 5").fetchall()
        if missing or not self.count:
            raise ValueError(f"Unpaired archive members or empty archive: {missing}")
