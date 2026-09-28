"""WutheringWaves 2.2: paired WAV/LAB in four solid 7z archives."""

import os
import tempfile
from pathlib import PurePosixPath
from urllib.parse import quote

import py7zr
from py7zr.io import Py7zIO, WriterFactory

from ..schema import make_record
from ._archives import safe_member

LANGUAGES = {
    "CN": ("zh", "中文 - Chinese"),
    "EN": ("en", "英语 - English"),
    "JP": ("ja", "日语 - Japanese"),
    "KR": ("ko", "韩语 - Korean"),
}


def input_files(root, config=None):
    if config not in (None, "all", *LANGUAGES):
        raise ValueError("Supported config: all/CN/EN/JP/KR")
    files = sorted(root.glob("WutheringWaves2.2_*.7z"))
    for path in files:
        if path.stem.rsplit("_", 1)[1] not in LANGUAGES:
            raise ValueError(f"Unknown Wuthering language: {path.name}")
    return files if config in (None, "all") else [p for p in files if p.stem.endswith("_" + config)]


class SliceIO(Py7zIO):
    """Disjoint byte ranges in one temp file; no per-utterance extracted files."""

    def __init__(self, fd, offset, length):
        self.fd, self.offset, self.length = fd, offset, length
        self.position = self.written = 0
        self.complete = False

    def write(self, data):
        if self.position + len(data) > self.length:
            raise ValueError("7z output exceeds declared member size")
        count = os.pwrite(self.fd, data, self.offset + self.position)
        if count != len(data):
            raise OSError("Short write to 7z temporary spool")
        self.position += count
        self.written = max(self.written, self.position)
        return count

    def read(self, size=None):
        size = (
            self.length - self.position
            if size is None or size < 0
            else min(size, self.length - self.position)
        )
        data = os.pread(self.fd, size, self.offset + self.position)
        self.position += len(data)
        return data

    def seek(self, offset, whence=0):
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.length)
        if not 0 <= position <= self.length:
            raise ValueError("7z seek outside member")
        self.position = position
        return position

    def flush(self):
        pass

    def size(self):
        return self.written

    def close(self):
        if self.written != self.length:
            raise ValueError("Incomplete 7z member")
        self.complete = True


class SpoolFactory(WriterFactory):
    def __init__(self, fd, members):
        self.products = {}
        offset = 0
        for name, length in members.items():
            self.products[name] = SliceIO(fd, offset, length)
            offset += length
        self.created = set()

    def create(self, filename):
        if filename not in self.products or filename in self.created:
            raise ValueError(f"Unexpected or duplicate 7z extraction member: {filename}")
        self.created.add(filename)
        return self.products[filename]

    def read(self, name):
        stream = self.products[name]
        if not stream.complete:
            raise ValueError(f"7z member did not finish extraction: {name}")
        stream.seek(0)
        data = stream.read()
        if len(data) != stream.length:
            raise ValueError("Truncated 7z temporary spool")
        return data


def iter_records(root, snapshot, *, files=None):
    yield from _records(root, snapshot, files=files)


def iter_preview_records(root, snapshot, *, files, limit):
    # Selecting only preview pairs avoids writing entire 10GB solid archives to temp storage.
    yield from _records(root, snapshot, files=files, limit=limit)


def _records(root, snapshot, *, files=None, limit=None):
    remaining = limit
    for path in files if files is not None else input_files(root):
        code = path.stem.rsplit("_", 1)[1]
        language, directory = LANGUAGES[code]
        with py7zr.SevenZipFile(path, "r") as archive:
            members = {}
            for info in archive.list():
                name = safe_member(info.filename)
                if info.is_symlink or (not info.is_directory and not info.is_file):
                    raise ValueError(f"Unsupported 7z member: {name}")
                if info.is_directory:
                    continue
                if name != info.filename or name in members or not name.endswith((".wav", ".lab")):
                    raise ValueError(f"Unexpected or duplicate Wuthering member: {name}")
                parts = PurePosixPath(name).parts
                if len(parts) < 3 or parts[0] != directory:
                    raise ValueError(f"Wuthering language/role path mismatch: {name}")
                members[name] = info.uncompressed
            wavs = [n for n in members if n.endswith(".wav")]
            labs = {n for n in members if n.endswith(".lab")}
            if not wavs or {n[:-4] + ".lab" for n in wavs} != labs:
                raise ValueError("Unpaired Wuthering WAV/LAB members")
            selected = wavs if remaining is None else wavs[:remaining]
            targets = [n for wav in selected for n in (wav, wav[:-4] + ".lab")]
            with tempfile.TemporaryFile(prefix="tts-7z-") as spool:
                factory = SpoolFactory(spool.fileno(), {n: members[n] for n in targets})
                archive.extract(targets=targets, factory=factory)
                for name in selected:
                    lab = name[:-4] + ".lab"
                    raw_text = factory.read(lab).decode("utf-8-sig")
                    text = raw_text.strip() or None
                    parts = PurePosixPath(name).parts
                    role = parts[1]
                    known = role.lower() not in {"none", "unknown", "null", "n/a"} and bool(
                        role.strip("?？ ")
                    )
                    yield make_record(
                        dataset_id="wutheringwaves",
                        source_snapshot=snapshot,
                        source_key=name,
                        source_locator={
                            "root": "raw_tts",
                            "path": f"WutheringWaves-2.2/{path.relative_to(root)}",
                            "member": name,
                            "metadata_member": lab,
                        },
                        audio_bytes=factory.read(name),
                        audio_name=parts[-1],
                        text=text,
                        text_kind="source_transcript" if text else None,
                        language=language,
                        speaker_id=f"wutheringwaves:{language}:{quote(role, safe='')}"
                        if known
                        else None,
                        speaker_scope="dataset" if known else None,
                        source_config=code,
                        metadata={
                            "original_split": None,
                            "original_config": code,
                            "upstream_role": role,
                            "speaker_label_kind": "character_per_language",
                            "upstream_lab": raw_text,
                            "category_path": list(parts[2:-1]),
                        },
                    )
            if remaining is not None:
                remaining -= len(selected)
                if remaining <= 0:
                    return
