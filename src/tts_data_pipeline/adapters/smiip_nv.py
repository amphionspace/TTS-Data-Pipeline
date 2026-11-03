"""SMIIP-NV: 37 Mandarin speakers reading scripts with laughter, coughs and crying.

SMIIP_NV.zip holds speaker/emotion folders with one annotation.txt per folder: a line is
`<wav>\\t<text with tags>\\t[start, end],...`. Most clips have a point-style line (`<crying>`
where the event occurs, one interval per tag); some also or only have a span-style line
(`<crying/>...</crying>` over speech produced while crying). The point line is the text when
present, otherwise the first span line; every line stays in metadata with its intervals and
the others become variants. SMIIP_NV_SPK_finetune.zip repacks the same audio (identical CRCs)
into spk_train/spk_test; only its split assignment is read, as the original split.
"""

import posixpath
import re
import zipfile

from ..schema import make_record
from ._archives import safe_member

MAIN = "SMIIP_NV.zip"
SPLITS = "SMIIP_NV_SPK_finetune.zip"
EMOTIONS = {"高兴": "happy", "伤心": "sad", "中性": "neutral", "生气": "angry", "惊讶": "surprised"}
AUDIO = re.compile(r"SMIIP_NV_Up/(MIC-\d{4})/([^/]+)/((MIC-\d{4})_\d{4})\.wav")
ANNOTATION = re.compile(r"SMIIP_NV_Up/(MIC-\d{4})/([^/]+)/annotation\.txt")
SPLIT_AUDIO = re.compile(r"SMIIP_NV_SPK_finetune/(spk_train|spk_test)/(MIC-\d{4}_\d{4})\.(wav|txt)")
INTERVAL = re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]")


def input_files(root, config=None):
    if config not in (None, "all", "default"):
        raise ValueError("SMIIP-NV only supports config=all")
    if not (root / MAIN).is_file() or not (root / SPLITS).is_file():
        raise ValueError("SMIIP-NV needs SMIIP_NV.zip and SMIIP_NV_SPK_finetune.zip")
    return [root / MAIN]


def dependency_files(root, path):
    """The repacked archive only contributes the train/test assignment."""
    return [root / SPLITS]


def member_name(info):
    """Upstream zips store GBK names without the UTF-8 flag."""
    name = info.filename if info.flag_bits & 0x800 else info.filename.encode("cp437").decode("gbk")
    return safe_member(name)


def intervals(value, where):
    """`[a, b],[c, d]` in seconds; one clip has an empty list."""
    compact = value.replace(" ", "")
    pairs = INTERVAL.findall(compact)
    found = [[float(a), float(b)] for a, b in pairs]
    if compact != ",".join(f"[{a},{b}]" for a, b in pairs) or any(a > b for a, b in found):
        raise ValueError(f"Unexpected SMIIP-NV interval list: {where}")
    return found


def span_style(text):
    return "/>" in text or "</" in text


def annotations(archive, members):
    found = {}
    for name, info in members.items():
        match = ANNOTATION.fullmatch(name)
        if match is None:
            continue
        folder = posixpath.dirname(name)
        for number, line in enumerate(archive.read(info).decode("utf-8-sig").splitlines()):
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 3 or not fields[0].endswith(".wav"):
                raise ValueError(f"Unexpected SMIIP-NV annotation line: {name}:{number}")
            text = fields[1].strip()
            if not text:
                raise ValueError(f"Empty SMIIP-NV annotation text: {name}:{number}")
            found.setdefault(posixpath.join(folder, fields[0]), []).append(
                {
                    "text": text,
                    "intervals": intervals(fields[2], f"{name}:{number}"),
                    "style": "span" if span_style(text) else "point",
                    "member": name,
                    "line": number,
                }
            )
    return found


def split_assignment(path):
    splits = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            match = SPLIT_AUDIO.fullmatch(member_name(info))
            if match is None:
                raise ValueError(f"Unexpected SMIIP-NV split member: {info.filename}")
            split, key, suffix = match.groups()
            if suffix == "wav":
                if key in splits:
                    raise ValueError(f"Duplicate SMIIP-NV split audio: {key}")
                splits[key] = (split, info.CRC)
    return splits


def iter_records(root, snapshot, *, files=None):
    files = files if files is not None else input_files(root)
    if [p.name for p in files] != [MAIN]:
        raise ValueError("SMIIP-NV converts SMIIP_NV.zip as one input")
    splits = split_assignment(root / SPLITS)
    with zipfile.ZipFile(files[0]) as archive:
        members = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = member_name(info)
            if name in members:
                raise ValueError(f"Duplicate SMIIP-NV member: {name}")
            members[name] = info
        notes = annotations(archive, members)
        audio = {n: AUDIO.fullmatch(n) for n in members if not ANNOTATION.fullmatch(n)}
        if None in audio.values():
            raise ValueError(
                f"Unexpected SMIIP-NV member: {next(n for n, m in audio.items() if not m)}"
            )
        if set(notes) - set(audio):
            raise ValueError("SMIIP-NV annotation without audio")
        keys = [m.group(3) for m in audio.values()]
        if sorted(keys) != sorted(splits) or len(set(keys)) != len(keys):
            raise ValueError("SMIIP-NV archives do not hold the same clips")
        for name, match in audio.items():
            speaker, folder, key, prefix = match.groups()
            if prefix != speaker or folder not in EMOTIONS:
                raise ValueError(f"Unexpected SMIIP-NV clip path: {name}")
            split, crc = splits[key]
            if crc != members[name].CRC:
                raise ValueError(f"SMIIP-NV split archive audio differs: {key}")
            lines = notes.get(name, [])
            points = [a for a in lines if a["style"] == "point"]
            if len(points) > 1:
                raise ValueError(f"Several SMIIP-NV point annotations: {key}")
            main = (points or lines or [None])[0]
            text = main["text"] if main else None
            yield make_record(
                dataset_id="smiip_nv",
                source_snapshot=snapshot,
                source_key=key,
                source_locator={
                    "root": "raw_tts",
                    "path": f"SMIIP-NV/{MAIN}",
                    "member": name,
                    "metadata_member": main["member"] if main else None,
                },
                audio_bytes=archive.read(members[name]),
                audio_name=posixpath.basename(name),
                text=text,
                text_kind="source_transcript_paralinguistic" if text else None,
                language="zh",
                speaker_id=f"smiip_nv:{speaker}",
                speaker_scope="dataset",
                source_config="all",
                source_split="train",
                text_variants=[
                    {
                        "kind": f"source_transcript_paralinguistic_{a['style']}",
                        "text": a["text"],
                        "language": "zh",
                    }
                    for a in lines
                    if a is not main
                ],
                metadata={
                    "emotion": EMOTIONS[folder],
                    "annotations": [
                        {k: a[k] for k in ("text", "intervals", "style", "line")} for a in lines
                    ],
                    "text_style": main["style"] if main else None,
                    "missing_annotation": main is None,
                    "upstream_speaker": speaker,
                    "original_split": split,
                    "original_split_group": "test" if split == "spk_test" else "train",
                },
            )
