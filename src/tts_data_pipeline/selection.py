"""Supervised selection: bounded fragment scans and globally consistent branches.

All row arrays are scratch state, never a published selection representation.
Rules without `annotation_inputs`/`text_filter` reproduce the first selection exactly;
selection-filter-v1 adds pinned ASR text sources, strict ASR language agreement, text
length/repetition policy and sample-scoped source-timeline evidence. Reference pairing
is deliberately unsupported.
"""

import csv
import functools
import hashlib
import json
import math
import multiprocessing
import os
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa

from .contract import schema_description
from .schema import digest
from .timestamps import BEIJING

COLUMNS = [
    "sample_id",
    "audio_sha256",
    "text",
    "language",
    "num_frames",
    "sample_rate",
    "duration_seconds",
    "parent_sample_id",
    "segment_start_frame",
    "segment_end_frame",
]
ROW = np.dtype(
    [
        ("sample_id", "S64"),
        ("audio_sha256", "S64"),
        ("text_hash", "S32"),
        ("language", "u1"),
        ("reason", "<u2"),
        ("flags", "<u4"),
        ("duration", "<f8"),
        ("chars", "<u4"),
        ("repetition", "<f4"),
        ("text_source", "u1"),
    ]
)
INDEX = np.dtype([("audio_sha256", "S64"), ("dataset", "<u2"), ("position", "<u8")])
REASONS = {
    0: "selected",
    101: "missing_audio",
    102: "decode_failed",
    103: "invalid_audio",
    104: "invalid_timeline",
    105: "source_timeline_unverified",
    1001: "missing_text",
    1002: "confirmed_mismatch",
    1003: "text_source_unavailable",
    1004: "text_repetition",
    1005: "text_too_dense",
    1006: "text_too_sparse",
    1007: "text_markup",
    1008: "asr_chunk_join",
    2001: "missing_language",
    2002: "unsupported_language",
    2003: "language_evidence_conflict",
    2004: "text_script_mismatch",
    3001: "below_duration_policy",
    3002: "above_duration_policy",
    4001: "duplicate_not_selected",
    4002: "conflicting_duplicate",
    5001: "reference_identity_unavailable",
    7001: "held_out",
    65535: "pending",
}
FLAGS = [
    "audio_issue",
    "missing_text",
    "pairing_issue",
    "language_policy",
    "duration_policy",
    "duplicate_member",
    "duplicate_conflict",
    "reference_policy",
    "held_out",
    "confirmed_exclusion",
    "source_timeline",
    "text_policy",
    "language_evidence",
]
ASR_COLUMNS = {
    "sample_id": "sample_id",
    "status": "status",
    "error_code": "error_code",
    "audio_sha256": "result.audio_sha256",
    "source_language": "result.source_language",
    "text": "result.text",
    "language": "result.language",
    "language_mismatch": "result.language_mismatch",
    "candidate_text": "result.language_candidate_text",
    "candidate_response": "result.language_candidate_response_json",
}


def effective_characters(text):
    """NFC code points of Unicode category L* or N* (str.isalnum is exactly that set)."""
    return "".join(filter(str.isalnum, unicodedata.normalize("NFC", text)))


def ngram_coverage(units, n, min_count):
    """Largest union coverage of any n-gram occurring >= min_count times (overlaps allowed)."""
    starts = {}
    key = (
        (lambda i: units[i : i + n])
        if isinstance(units, str)
        else (lambda i: tuple(units[i : i + n]))
    )
    for i in range(len(units) - n + 1):
        starts.setdefault(key(i), []).append(i)
    best = 0.0
    for positions in starts.values():
        if len(positions) < min_count:
            continue
        covered, end = 0, 0
        for s in positions:
            covered += s + n - max(s, end)
            end = s + n
        best = max(best, covered / len(units))
    return best


def repetition(text, language, config):
    """Return (path, coverage); path is None when the text is too short for either path."""
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    if language not in config["char_languages"]:
        words = [w for w in (effective_characters(t) for t in folded.split()) if w]
        if len(words) >= config["word_min_units"]:
            return "word", ngram_coverage(words, config["word_n"], config["min_count"])
    chars = effective_characters(folded)
    if len(chars) >= config["char_min_units"]:
        return "char", ngram_coverage(chars, config["char_n"], config["min_count"])
    return None, math.nan


@functools.lru_cache(maxsize=None)
def script_of(char):
    """Writing system of one letter: han, kana, hangul, latin, or the Unicode name's first word."""
    name = unicodedata.name(char, "")
    if name.startswith(("CJK", "IDEOGRAPHIC")):
        return "han"
    if name.startswith(("HIRAGANA", "KATAKANA")):
        return "kana"
    if name.startswith("HANGUL"):
        return "hangul"
    return "latin" if "LATIN" in name else name.split(" ", 1)[0]


def script_share(text, language, config):
    """Share of letter units in the language's expected scripts; None without letters.

    Han, kana and Hangul count one unit per character, other scripts one unit per word.
    """
    units, previous = [], None
    for char in unicodedata.normalize("NFC", text):
        if not char.isalpha():
            previous = None
            continue
        script = script_of(char)
        if script in ("han", "kana", "hangul") or script != previous:
            units.append(script)
        previous = script
    if not units:
        return None
    expected = config["expected"].get(language, config["expected"]["default"])
    return sum(u in expected for u in units) / len(units)


def chosen_text(raw_text, asr):
    """Selected text before analysis: stripped base text, or stripped successful ASR text."""
    if asr is None:
        return raw_text.strip() if raw_text is not None else ""
    if asr["status"] != "ok":
        return ""
    return (asr["text"] or "").strip()


def now():
    return datetime.now(BEIJING).isoformat()


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    os.replace(temporary, path)


def event(work, phase, **values):
    row = {"at": now(), "phase": phase, **values}
    print(json.dumps(row, ensure_ascii=False), flush=True)
    write_json(Path(work) / "status.json", row)


def open_base(root, source):
    path = Path(root) / source["manifest_path"]
    if file_sha(path) != source["manifest_sha256"]:
        raise ValueError("Base manifest changed")
    ds = lance.dataset(Path(root) / source["table_path"], version=source["lance_version"])
    if ds.count_rows() != source["rows"]:
        raise ValueError("Base row count changed")
    return ds


def known_exclusions(root, inputs, rules):
    """Pin the exact previously reviewed records, not a broad source-name blacklist."""
    rows = []
    for source in inputs:
        name = source["dataset_id"]
        if name not in {"wutheringwaves", "galgame"}:
            continue
        ds = open_base(root, source)
        condition = "num_frames = 1" if name == "wutheringwaves" else "duration_seconds > 300"
        found = ds.to_table(
            columns=[
                "sample_id",
                "audio_sha256",
                "text",
                "num_frames",
                "sample_rate",
                "duration_seconds",
            ],
            filter=condition,
        ).to_pylist()
        if len(found) != {"wutheringwaves": 32, "galgame": 7}[name]:
            raise ValueError(f"Reviewed exclusion scope changed: {name}: {len(found)}")
        for row in found:
            if name == "galgame" and row["text"].strip() not in {
                "て",
                "音声ファイルを再生しています。再生終了後メッセージ送りの操作をすることで先に進みます。",
            }:
                raise ValueError(f"Unreviewed long Galgame text: {row}")
            key = (
                {"audio_sha256": row["audio_sha256"]}
                if name == "wutheringwaves"
                else {
                    "dataset_id": name,
                    "release_id": source["release_id"],
                    "sample_id": row["sample_id"],
                    "text_revision": digest(
                        ["selected-text-v1", row["text"], rules["text_normalization"]]
                    ),
                }
            )
            item = {
                "exclusion_id": digest(["selection-exclusion-v1", key]),
                "scope": key,
                "reason": 103 if name == "wutheringwaves" else 1002,
                "evidence": {
                    "dataset_id": name,
                    "base_manifest_sha256": source["manifest_sha256"],
                    "record": row,
                },
                "purpose": "supervised_tts",
                "recorded_at": now(),
            }
            rows.append(item)
    # Several one-frame records may reference identical audio bytes.
    return list({r["exclusion_id"]: r for r in rows}.values())


def inherited_exclusions(root, parent_id):
    """Complete parent inheritance; the effective list must match its pinned hash."""
    folder = Path(root) / "selections" / parent_id
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest["status"] != "complete" or manifest["selection_id"] != parent_id:
        raise ValueError("Parent selection is not a complete publication")
    path = folder / manifest["exclusions"]["effective_path"]
    if file_sha(path) != manifest["exclusions"]["effective_sha256"]:
        raise ValueError("Parent effective exclusions changed")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    if len(rows) != manifest["exclusions"]["effective_count"]:
        raise ValueError("Parent effective exclusion count mismatch")
    parent = {
        "selection_id": parent_id,
        "manifest_path": str(Path("selections") / parent_id / "manifest.json"),
        "manifest_sha256": file_sha(folder / "manifest.json"),
        "exclusions_sha256": manifest["exclusions"]["effective_sha256"],
        "exclusions": len(rows),
    }
    return parent, rows


def timeline_exclusions(rules_path, rules, inputs):
    """Sample-scoped source-duration findings from the pinned interval audit."""
    evidence = rules.get("source_timeline_evidence")
    if not evidence:
        return []
    repository = Path(__file__).resolve().parents[2]
    path = repository / evidence["path"]
    if file_sha(path) != evidence["sha256"]:
        raise ValueError("Source timeline evidence changed")
    releases = {s["dataset_id"]: s for s in inputs}
    ignored = set(evidence["ignore_findings"])
    rows = []
    with path.open(newline="") as stream:
        for record in csv.DictReader(stream, delimiter="\t"):
            findings = set(record["findings"].split(","))
            if findings <= ignored:
                continue
            source = releases[record["dataset"]]
            key = {
                "dataset_id": record["dataset"],
                "release_id": source["release_id"],
                "sample_id": record["sample_id"],
            }
            rows.append(
                {
                    "exclusion_id": digest(["selection-exclusion-v1", key, 105]),
                    "scope": key,
                    "reason": 105,
                    "evidence": {
                        "path": evidence["path"],
                        "sha256": evidence["sha256"],
                        "record": record,
                    },
                    "purpose": "supervised_tts",
                    "recorded_at": now(),
                }
            )
    if len(rows) != evidence["expected_rows"]:
        raise ValueError(f"Source timeline evidence scope changed: {len(rows)}")
    return rows


def pin_annotations(root, rules, inputs):
    """Fix each adopted transcription run to its manifest, table snapshot and base inputs."""
    releases = {s["dataset_id"]: s for s in inputs}
    pinned, covered = [], set()
    for item in rules.get("annotation_inputs", []):
        folder = Path("annotations") / item["task"] / item["run_id"]
        manifest = json.loads((Path(root) / folder / "manifest.json").read_text())
        if manifest["status"] != "complete" or manifest["run_id"] != item["run_id"]:
            raise ValueError("Annotation input is not a complete publication")
        bases = {s["dataset_id"]: s for s in manifest["inputs"]}
        outputs = {o["dataset_id"]: o for o in manifest["outputs"]}
        for name in item["datasets"]:
            if name in covered:
                raise ValueError("Dataset has two text sources")
            covered.add(name)
            source, table = releases[name], outputs[name]["table"]
            if (
                bases[name]["manifest_sha256"] != source["manifest_sha256"]
                or bases[name]["lance_version"] != source["lance_version"]
                or table["rows"] != source["rows"]
                or table["branch"] is not None
            ):
                raise ValueError(f"Annotation was computed on a different base: {name}")
            pinned.append(
                {
                    "text_source": item["text_source"],
                    "task": item["task"],
                    "run_id": item["run_id"],
                    "manifest_path": str(folder / "manifest.json"),
                    "manifest_sha256": file_sha(Path(root) / folder / "manifest.json"),
                    "profile_id": manifest["profile_id"],
                    "dataset_id": name,
                    "release_id": source["release_id"],
                    "table_path": table["table_path"],
                    "branch": None,
                    "lance_version": table["lance_version"],
                    "rows": table["rows"],
                    "columns": sorted(ASR_COLUMNS.values()),
                    "base_manifest_sha256": source["manifest_sha256"],
                    "base_lance_version": source["lance_version"],
                }
            )
    return pinned


def align_annotations(work):
    """Write each pinned annotation in base row order, sliced like the scan tasks."""
    p = load_plan(work)
    folder = Path(work) / "annotations"
    folder.mkdir(exist_ok=True)
    checkpoint = folder / "aligned.json"
    if checkpoint.exists():
        done = json.loads(checkpoint.read_text())
        for item in done.values():
            if file_sha(item["path"]) != item["sha256"]:
                raise ValueError("Aligned annotation scratch changed")
        return done
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    done = {}
    for item in p["annotation_inputs"]:
        name = item["dataset_id"]
        source = next(s for s in p["inputs"] if s["dataset_id"] == name)
        base = open_base(p["root"], source)
        ids = base.to_table(columns=["sample_id"], scan_in_order=True)["sample_id"].cast(
            pa.large_string()
        )
        table = lance.dataset(Path(p["root"]) / item["table_path"], version=item["lance_version"])
        if table.count_rows() != item["rows"]:
            raise ValueError("Annotation row count changed")
        names = list(ASR_COLUMNS)
        ann = table.to_table(columns=[ASR_COLUMNS[k] for k in names]).rename_columns(names)
        # 64-bit offsets: one dataset's aligned text can exceed 2 GiB.
        ann = ann.cast(
            pa.schema(
                [
                    f.with_type(pa.large_string()) if pa.types.is_string(f.type) else f
                    for f in ann.schema
                ]
            )
        )
        if pc.count_distinct(ann["sample_id"]).as_py() != len(ann):
            raise ValueError("Annotation sample IDs are not unique")
        index = pc.index_in(ids, value_set=ann["sample_id"])
        if index.null_count:
            raise ValueError(f"Annotation misses base rows: {name}: {index.null_count}")
        aligned = ann.take(index)
        if not aligned["sample_id"].equals(ids):
            raise ValueError("Annotation alignment failed")
        for task in (t for t in p["tasks"] if t["dataset"] == name):
            path = folder / f"{name}-{task['offset']:012d}.parquet"
            pq.write_table(aligned.slice(task["offset"], task["rows"]), path)
            done[path.stem] = {"path": str(path), "sha256": file_sha(path), "rows": task["rows"]}
        event(work, "annotation_aligned", dataset=name, rows=len(aligned))
    write_json(checkpoint, done)
    return done


def asr_rows(p, task):
    """Aligned annotation rows for one scan task, or None for base-text datasets."""
    if not any(a["dataset_id"] == task["dataset"] for a in p["annotation_inputs"]):
        return None
    import pyarrow.parquet as pq

    path = Path(p["work"]) / "annotations" / f"{task['dataset']}-{task['offset']:012d}.parquet"
    done = json.loads((Path(p["work"]) / "annotations" / "aligned.json").read_text())
    if file_sha(path) != done[path.stem]["sha256"]:
        raise ValueError("Aligned annotation scratch changed")
    return pq.read_table(path).to_pylist()


def plan(root, work, rules_path, *, selection_id=None):
    root, work = Path(root).resolve(), Path(work).resolve()
    if not work.is_relative_to(Path("/tmp")):
        raise ValueError("Selection scratch must be under /tmp")
    work.mkdir(parents=True, exist_ok=True)
    if (work / "plan.json").exists():
        raise ValueError("Existing plan; resume its explicit scan/publish phases")
    rules = json.loads(Path(rules_path).read_text())
    parent_id = rules.get("parent_selection")
    if not parent_id and list((root / "selections").glob("*/manifest.json")):
        raise ValueError(
            "Existing selections need explicit exclusion inheritance; "
            "first-root executor refuses reset"
        )
    if rules["reference_policy"] != "self":
        raise ValueError("This executor supports self-reference selection only")
    selection_id = selection_id or datetime.now(BEIJING).strftime(
        "tts-selection-supervised-tts-%Y%m%dT%H%M%Sbjt-01"
    )
    inputs, tasks = [], []
    for name in rules["source_priority"]:
        manifest_path = Path("datasets") / name / "v0.1" / "manifest.json"
        m = json.loads((root / manifest_path).read_text())
        if m["status"] != "complete" or m["contract_version"] != "v0.1":
            raise ValueError(f"Not a published base: {name}")
        source = {
            "dataset_id": name,
            "release_id": "v0.1",
            "manifest_path": str(manifest_path),
            "manifest_sha256": file_sha(root / manifest_path),
            "table_path": str(manifest_path.parent / m["table_path"]),
            "branch": None,
            "lance_version": m["lance_version"],
            "rows": m["rows"],
        }
        ds = open_base(root, source)
        if selection_id in ds.branches.list():
            raise ValueError("Selection ID already names a branch; use its existing plan to resume")
        if lance.dataset(root / source["table_path"]).version != source["lance_version"]:
            raise ValueError("Unexpected newer base main; review before publishing selection")
        inputs.append(source)
        offset, group, size = 0, [], 0
        for fragment in ds.get_fragments():
            count = fragment.count_rows()
            if count != fragment.physical_rows:
                raise ValueError("Base has deleted rows; row-address layout must be reviewed")
            group.append(fragment.fragment_id)
            size += count
            if size >= 250_000:
                tasks.append({"dataset": name, "offset": offset, "rows": size, "fragments": group})
                offset += size
                group, size = [], 0
        if group:
            tasks.append({"dataset": name, "offset": offset, "rows": size, "fragments": group})
            offset += size
        if offset != source["rows"]:
            raise ValueError("Fragment row counts do not cover base")
    if parent_id:
        parent, exclusions = inherited_exclusions(root, parent_id)
        additions = timeline_exclusions(Path(rules_path).resolve(), rules, inputs)
        known = {e["exclusion_id"] for e in exclusions}
        additions = [e for e in additions if e["exclusion_id"] not in known]
    else:
        parent, exclusions, additions = None, known_exclusions(root, inputs, rules), []
    result = {
        "selection_id": selection_id,
        "root": str(root),
        "work": str(work),
        "created_at": now(),
        "rules": rules,
        "rules_sha256": digest(rules),
        "inputs": inputs,
        "annotation_inputs": pin_annotations(root, rules, inputs),
        "parent": parent,
        "tasks": tasks,
        "exclusions": exclusions + additions,
        "exclusion_additions": [e["exclusion_id"] for e in additions],
        "implementation_sha256": file_sha(__file__),
    }
    write_json(work / "plan.json", result)
    event(
        work,
        "planned",
        selection_id=selection_id,
        datasets=len(inputs),
        tasks=len(tasks),
        rows=sum(s["rows"] for s in inputs),
        exclusions=len(exclusions),
    )
    return result


def asr_language_conflict(asr, language, source):
    """Strict agreement: one mapped ASR label equal to the selected source language."""
    if asr["language_mismatch"] or asr["candidate_text"] is not None:
        return True
    if asr["candidate_response"] is not None:
        return True
    return source["language_labels"].get(asr["language"] or "") != language


def classify(row, dataset, rules, audio_exclusions, sample_exclusions, asr=None, evidence=None):
    reasons, flags = [], 0
    raw_text = row["text"]
    text = chosen_text(raw_text, asr)
    language = rules["language_aliases"].get(row["language"], row["language"])
    if row["audio_sha256"] in audio_exclusions:
        reasons.append(103)
        flags |= (1 << 0) | (1 << 9)
    entries = sample_exclusions.get((dataset, row["sample_id"])) or []
    for entry in [entries] if isinstance(entries, dict) else entries:
        if entry.get("reason", 1002) == 105:
            reasons.append(105)
            flags |= 1 << 10
            continue
        revision = digest(["selected-text-v1", raw_text, rules["text_normalization"]])
        if asr is not None or entry["scope"]["text_revision"] != revision:
            raise ValueError("Exclusion text revision mismatch")
        reasons.append(1002)
        flags |= (1 << 2) | (1 << 9)
    if not row["audio_sha256"]:
        reasons.append(101)
        flags |= 1
    source = None
    if asr is not None:
        source = rules["text_sources"][str(asr["text_source"])]
        if asr["sample_id"] != row["sample_id"]:
            raise ValueError("Annotation row is not aligned with base")
        if asr["status"] == "ok" and (
            asr["audio_sha256"] != row["audio_sha256"] or asr["source_language"] != row["language"]
        ):
            raise ValueError("Annotation identity disagrees with base")
        if asr["status"] != "ok":
            reasons.append(1003)
            flags |= 1 << 1
    chars = effective_characters(text)
    filtering = rules.get("text_filter")
    if not text or (filtering and len(chars) < filtering["min_effective_characters"]):
        if 1003 not in reasons:
            reasons.append(1001)
        flags |= 1 << 1
    if language not in rules["supported_languages"]:
        reasons.append(2001 if not language else 2002)
        flags |= 1 << 3
    if asr is not None and asr["status"] == "ok" and asr_language_conflict(asr, language, source):
        reasons.append(2003)
        flags |= (1 << 3) | (1 << 12)
    if filtering and language in rules["supported_languages"]:
        share = script_share(text, language, filtering["script"])
        if share is not None and share < filtering["script"]["min_share"]:
            reasons.append(2004)
            flags |= (1 << 3) | (1 << 12)
    if filtering and any(c in filtering["markup_characters"] for c in text):
        reasons.append(1007)
        flags |= 1 << 11
    if filtering and asr is not None and "\n" in text:
        # vLLM joins internally chunked long audio with newlines; text breaks at each cut.
        reasons.append(1008)
        flags |= 1 << 11
    path, coverage = None, math.nan
    if filtering and chars:
        config = filtering["repetition"]
        path, coverage = repetition(text, language, config)
        if path and coverage >= config["min_coverage"]:
            reasons.append(1004)
            flags |= 1 << 11
        group = "dense_languages" if language in filtering["dense_languages"] else "other"
        low, high = filtering["seconds_per_character"][group]
        ratio = row["duration_seconds"] / len(chars)
        if ratio < low:
            reasons.append(1005)
            flags |= 1 << 11
        if ratio > high:
            reasons.append(1006)
            flags |= 1 << 11
    if evidence is not None:
        evidence.update(chars=len(chars), repetition_path=path, repetition=coverage)
    duration = row["duration_seconds"]
    if not np.isfinite(duration) or row["sample_rate"] <= 0 or row["num_frames"] <= 0:
        raise ValueError("Invalid base timeline")
    if abs(duration - row["num_frames"] / row["sample_rate"]) > 1e-9:
        raise ValueError("Base duration disagrees with native frame count")
    if any(
        row.get(k) is not None
        for k in ("parent_sample_id", "segment_start_frame", "segment_end_frame")
    ):
        raise ValueError("Derived input requires explicit view/timeline handling")
    bounds = rules["duration_seconds"]
    if duration < bounds["min_inclusive"]:
        reasons.append(3001)
        flags |= 1 << 4
    if duration > bounds["max_inclusive"]:
        reasons.append(3002)
        flags |= 1 << 4
    reason = next((r for r in rules["reason_priority"] if r in reasons), 0)
    return reason, flags, language, text


def scan_part(args):
    p, task = args
    work = Path(p["work"]) / "parts"
    work.mkdir(exist_ok=True)
    key = f"{task['dataset']}-{task['offset']:012d}"
    done, path = work / (key + ".json"), work / (key + ".npy")
    if done.exists():
        old = json.loads(done.read_text())
        if file_sha(path) != old["sha256"]:
            raise ValueError("Damaged selection scratch checkpoint")
        return old
    source = next(s for s in p["inputs"] if s["dataset_id"] == task["dataset"])
    ds = open_base(p["root"], source)
    fragments = [ds.get_fragment(i) for i in task["fragments"]]
    temporary = path.with_suffix(".incomplete.npy")
    array = np.lib.format.open_memmap(temporary, mode="w+", dtype=ROW, shape=(task["rows"],))
    rules = p["rules"]
    audio = {e["scope"]["audio_sha256"] for e in p["exclusions"] if "audio_sha256" in e["scope"]}
    samples = {}
    for entry in p["exclusions"]:
        if "sample_id" in entry["scope"]:
            # One sample may carry several independent exclusions.
            samples.setdefault((entry["scope"]["dataset_id"], entry["scope"]["sample_id"]), [])
            samples[(entry["scope"]["dataset_id"], entry["scope"]["sample_id"])].append(entry)
    annotation = asr_rows(p, task)
    text_source = next(
        (a["text_source"] for a in p["annotation_inputs"] if a["dataset_id"] == task["dataset"]),
        0,
    )
    languages = {lang: i + 1 for i, lang in enumerate(rules["supported_languages"])}
    counter, lang_counts, lang_seconds = Counter(), Counter(), Counter()
    raw_counts, raw_seconds = Counter(), Counter()
    checks = Counter()
    ids = hashlib.sha256()
    position = whitespace = 0
    # Lance 12 to_batches accepts **kwargs but drops `fragments`; use scanner directly.
    for batch in ds.scanner(
        columns=COLUMNS,
        fragments=fragments,
        scan_in_order=True,
        batch_size=8192,
        fragment_readahead=1,
    ).to_batches():
        for row in batch.to_pylist():
            asr = None
            if annotation is not None:
                asr = annotation[position] | {"text_source": text_source}
            evidence = {}
            reason, flags, lang, text = classify(
                row, task["dataset"], rules, audio, samples, asr, evidence
            )
            sid, sha = row["sample_id"], row["audio_sha256"]
            if len(sid) != 64 or any(c not in "0123456789abcdef" for c in sid):
                raise ValueError("Invalid sample ID")
            if sha and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
                raise ValueError("Invalid full audio SHA256")
            array[position] = (
                sid.encode(),
                (sha or "").encode(),
                hashlib.sha256(text.encode()).digest(),
                languages.get(lang, 0),
                reason,
                flags,
                row["duration_seconds"],
                evidence["chars"],
                evidence["repetition"],
                text_source,
            )
            checks["repetition_" + (evidence["repetition_path"] or "unchecked")] += 1
            checks["sample_exclusions"] += len(samples.get((task["dataset"], sid), []))
            if asr is not None:
                checks["asr_" + str(asr["status"])] += 1
            ids.update(sid.encode() + b"\n")
            counter[str(reason)] += 1
            lang_counts[lang or "<missing>"] += 1
            lang_seconds[lang or "<missing>"] += row["duration_seconds"]
            raw_counts[row["language"] or "<missing>"] += 1
            raw_seconds[row["language"] or "<missing>"] += row["duration_seconds"]
            whitespace += row["text"] is not None and text != row["text"]
            position += 1
    if position != task["rows"]:
        raise ValueError("Scan did not cover planned rows")
    array.flush()
    del array
    os.replace(temporary, path)
    result = task | {
        "file": str(path),
        "sha256": file_sha(path),
        "reasons": dict(counter),
        "language_rows": dict(lang_counts),
        "language_seconds": dict(lang_seconds),
        "raw_language_rows": dict(raw_counts),
        "raw_language_seconds": dict(raw_seconds),
        "outer_whitespace": whitespace,
        "checks": dict(checks),
        "ordered_ids_sha256": ids.hexdigest(),
    }
    write_json(done, result)
    return result


def load_plan(work):
    p = json.loads((Path(work) / "plan.json").read_text())
    if p["implementation_sha256"] != file_sha(__file__):
        raise ValueError("Executor changed since plan; review and create a new plan")
    return p


def scan(work, workers=64):
    p = load_plan(work)
    align_annotations(work)
    completed = []
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        jobs = [pool.submit(scan_part, (p, task)) for task in p["tasks"]]
        for job in as_completed(jobs):
            completed.append(job.result())
            event(
                work,
                "scan",
                tasks_done=len(completed),
                tasks_total=len(jobs),
                rows_done=sum(t["rows"] for t in completed),
                workers=workers,
            )
    write_json(
        Path(work) / "scan.json", sorted(completed, key=lambda t: (t["dataset"], t["offset"]))
    )
    event(work, "scan_complete", rows=sum(t["rows"] for t in completed), workers=workers)


def duplicate_schema():
    return pa.schema(
        [
            *[
                pa.field(k, pa.string())
                for k in (
                    "dataset_id",
                    "release_id",
                    "sample_id",
                    "audio_sha256",
                    "representative_dataset_id",
                    "representative_release_id",
                    "representative_sample_id",
                    "group_disposition",
                )
            ],
            pa.field("raw_text_conflict", pa.bool_()),
            pa.field("raw_language_conflict", pa.bool_()),
        ]
    )


def decide_group(members):
    """Return a deterministic representative and unresolved conflict, using eligible rows."""
    eligible = [(dataset, pos, row) for dataset, pos, row in members if row["reason"] == 0]
    # Pinned ASR text was produced from these exact audio bytes, so it supersedes base text
    # within the group; text conflicts are judged among the members carrying it.
    preferred = [m for m in eligible if m[2]["text_source"]] or eligible
    conflict = (
        len({bytes(row["text_hash"]) for _, _, row in preferred}) > 1
        or len({int(row["language"]) for _, _, row in eligible}) > 1
    )
    winner = (
        min(preferred, key=lambda item: (item[0], bytes(item[2]["sample_id"])))
        if (eligible and not conflict)
        else None
    )
    return winner, conflict


def resolve_duplicates(work):
    p = load_plan(work)
    work = Path(work)
    checkpoint = work / "duplicates.json"
    if checkpoint.exists():
        result = json.loads(checkpoint.read_text())
        for item in result["arrays"]:
            if file_sha(item["path"]) != item["sha256"]:
                raise ValueError("Selection decisions changed since checkpoint")
        event(work, "duplicates_reused", groups=result["groups"])
        return result
    parts = json.loads((work / "scan.json").read_text())
    applied = sum(t.get("checks", {}).get("sample_exclusions", 0) for t in parts)
    if applied != sum("sample_id" in e["scope"] for e in p["exclusions"]):
        raise ValueError("Sample-scoped exclusions do not all match a base row")
    sources = p["inputs"]
    arrays, array_paths = [], []
    total = sum(s["rows"] for s in sources)
    index = np.lib.format.open_memmap(
        work / "global-index.npy", mode="w+", dtype=INDEX, shape=(total,)
    )
    global_offset = 0
    for dataset, source in enumerate(sources):
        name = source["dataset_id"]
        path = work / (name + ".decisions.npy")
        array = np.lib.format.open_memmap(path, mode="w+", dtype=ROW, shape=(source["rows"],))
        offset = 0
        for part in sorted((t for t in parts if t["dataset"] == name), key=lambda t: t["offset"]):
            if part["offset"] != offset or file_sha(part["file"]) != part["sha256"]:
                raise ValueError("Scratch parts do not match plan")
            block = np.load(part["file"], mmap_mode="r")
            end = offset + len(block)
            array[offset:end] = block
            at = global_offset + offset
            index["audio_sha256"][at : at + len(block)] = block["audio_sha256"]
            index["dataset"][at : at + len(block)] = dataset
            index["position"][at : at + len(block)] = np.arange(offset, end, dtype=np.uint64)
            offset = end
        if offset != len(array):
            raise ValueError("Missing scan parts")
        array.flush()
        arrays.append(array)
        array_paths.append(path)
        global_offset += offset
    event(work, "global_sort", rows=total, scratch_index_bytes=index.nbytes)
    # Fixed-width mmap, not hundreds of millions of Python objects or SQL inserts.
    index.sort(order="audio_sha256", kind="quicksort")
    index.flush()
    pairs = np.flatnonzero(index["audio_sha256"][1:] == index["audio_sha256"][:-1])
    breaks = np.diff(pairs) > 1
    starts = pairs[np.r_[True, breaks]] if len(pairs) else []
    ends = pairs[np.r_[breaks, True]] + 2 if len(pairs) else []
    stats = {
        "groups": 0,
        "members": 0,
        "redundant_rows": 0,
        "conflicting_groups": 0,
        "raw_text_conflict_groups": 0,
        "raw_language_conflict_groups": 0,
    }
    duplicate_path = work / "duplicates.lance"
    if duplicate_path.exists():
        # Only this task's unpublished local scratch, never a shared or published table.
        import shutil

        shutil.rmtree(duplicate_path)

    def batches():
        pending = []
        for start, end in zip(starts, ends, strict=True):
            group = index[start:end]
            audio_hash = bytes(group[0]["audio_sha256"]).decode()
            if not audio_hash:
                continue
            members = [
                (
                    int(g["dataset"]),
                    int(g["position"]),
                    arrays[int(g["dataset"])][int(g["position"])].copy(),
                )
                for g in group
            ]
            winner, conflict = decide_group(members)
            raw_text = len({bytes(r["text_hash"]) for _, _, r in members if not r["flags"] & 2}) > 1
            raw_lang = len({int(r["language"]) for _, _, r in members if r["language"]}) > 1
            stats["groups"] += 1
            stats["members"] += len(members)
            stats["redundant_rows"] += len(members) - 1
            stats["conflicting_groups"] += conflict
            stats["raw_text_conflict_groups"] += raw_text
            stats["raw_language_conflict_groups"] += raw_lang
            for dataset, pos, row in members:
                flags = int(row["flags"]) | (1 << 5) | ((1 << 6) if conflict else 0)
                reason = int(row["reason"])
                if reason == 0:
                    if conflict:
                        reason = 4002
                    elif winner is not None and (dataset, pos) != winner[:2]:
                        reason = 4001
                arrays[dataset]["reason"][pos] = reason
                arrays[dataset]["flags"][pos] = flags
                pending.append(
                    {
                        "dataset_id": sources[dataset]["dataset_id"],
                        "release_id": "v0.1",
                        "sample_id": bytes(row["sample_id"]).decode(),
                        "audio_sha256": audio_hash,
                        "representative_dataset_id": sources[winner[0]]["dataset_id"]
                        if winner
                        else None,
                        "representative_release_id": "v0.1" if winner else None,
                        "representative_sample_id": bytes(winner[2]["sample_id"]).decode()
                        if winner
                        else None,
                        "group_disposition": "conflict"
                        if conflict
                        else ("selected" if winner else "no_eligible"),
                        "raw_text_conflict": raw_text,
                        "raw_language_conflict": raw_lang,
                    }
                )
            if len(pending) >= 8192:
                yield pa.RecordBatch.from_pylist(pending, schema=duplicate_schema())
                pending = []
            if stats["groups"] % 100000 == 0:
                event(work, "duplicate_decisions", **stats)
        if pending:
            yield pa.RecordBatch.from_pylist(pending, schema=duplicate_schema())

    table = lance.write_dataset(
        pa.RecordBatchReader.from_batches(duplicate_schema(), batches()),
        duplicate_path,
        data_storage_version="2.2",
        max_rows_per_file=1_000_000,
    )
    table.create_scalar_index("sample_id", "BTREE")
    table.create_scalar_index("audio_sha256", "BTREE")
    if table.count_rows() != stats["members"]:
        raise ValueError("Duplicate membership coverage mismatch")
    array_info = []
    for array, path in zip(arrays, array_paths, strict=True):
        array.flush()
        array_info.append({"path": str(path), "sha256": file_sha(path)})
    result = stats | {
        "arrays": array_info,
        "lance_version": table.version,
        "duplicate_path": str(duplicate_path),
    }
    write_json(checkpoint, result)
    event(work, "duplicates_complete", **stats)
    return result


def branch_files(ds):
    return {
        f"{frag.fragment_id}:{f.path}" for frag in ds.get_fragments() for f in frag.data_files()
    }


def text_override(raw, asr):
    """Only changed selected text is stored; null falls back to the base text."""
    if asr is None:
        return raw.strip() if raw is not None and raw != raw.strip() else None
    chosen = chosen_text(raw, asr)
    return chosen if chosen != raw else None


def aligned_text(p, name):
    """(text_source, status, text) arrays in base row order for an ASR-text dataset."""
    item = next((a for a in p["annotation_inputs"] if a["dataset_id"] == name), None)
    if item is None:
        return None
    import pyarrow.parquet as pq

    done = json.loads((Path(p["work"]) / "annotations" / "aligned.json").read_text())
    tables = []
    for task in sorted((t for t in p["tasks"] if t["dataset"] == name), key=lambda t: t["offset"]):
        entry = done[f"{name}-{task['offset']:012d}"]
        if file_sha(entry["path"]) != entry["sha256"]:
            raise ValueError("Aligned annotation scratch changed")
        tables.append(pq.read_table(entry["path"], columns=["status", "text"]))
    table = pa.concat_tables(tables).combine_chunks()
    return item["text_source"], table["status"], table["text"]


def overrides(batch_texts, positions, asr):
    if asr is None:
        values = [text_override(t, None) for t in batch_texts]
        return values, [0 if v is not None else None for v in values]
    code, status, text = asr
    index = pa.array(positions)
    rows = zip(status.take(index).to_pylist(), text.take(index).to_pylist(), strict=True)
    values = [
        text_override(raw, {"status": st, "text": t})
        for raw, (st, t) in zip(batch_texts, rows, strict=True)
    ]
    return values, [code if v is not None else None for v in values]


def publish_branch(args):
    p, source, array_info = args
    ds = open_base(p["root"], source)
    name = source["dataset_id"]
    asr = aligned_text(p, name)
    state = Path(p["work"]) / (name + ".branch.json")
    array = np.load(array_info["path"], mmap_mode="r")
    if file_sha(array_info["path"]) != array_info["sha256"]:
        raise ValueError("Changed decision array")
    branch_name = p["selection_id"]
    if state.exists():
        output = json.loads(state.read_text())
        branch = ds.checkout_version((branch_name, output["lance_version"]))
    else:
        branches = ds.branches.list()
        if branch_name in branches:
            branch = ds.checkout_version((branch_name, None))
        else:
            branch = ds.create_branch(branch_name, source["lance_version"])
        if "selection_reason" not in branch.schema.names:
            # Use stable row addresses to locate scratch; callbacks may execute out of order.
            offsets, offset = {}, 0
            for fragment in ds.get_fragments():
                offsets[fragment.fragment_id] = offset
                offset += fragment.count_rows()
            schema = pa.schema(
                [
                    ("selection_reason", pa.uint16()),
                    ("selection_flags", pa.uint32()),
                    ("selected_text", pa.string()),
                    ("selected_text_source", pa.uint32()),
                    ("selected_language", pa.string()),
                ]
            )

            @lance.batch_udf(output_schema=schema)
            def selection_columns(batch):
                address = batch.column("_rowaddr").to_numpy()
                fragment = address >> np.uint64(32)
                local = address & np.uint64(0xFFFFFFFF)
                positions = local.astype(np.int64)
                for fid in np.unique(fragment):
                    positions[fragment == fid] += offsets[int(fid)]
                expected = array[positions]
                actual_ids = np.asarray(batch.column("sample_id").to_pylist(), dtype="S64")
                if not np.array_equal(actual_ids, expected["sample_id"]):
                    raise ValueError("Base row address / sample identity mismatch")
                texts = batch.column("text").to_pylist()
                values, codes = overrides(texts, positions, asr)
                aliases = p["rules"]["language_aliases"]
                languages = batch.column("language").to_pylist()
                language_overrides = [
                    aliases[lang] if lang in aliases and aliases[lang] != lang else None
                    for lang in languages
                ]
                return pa.record_batch(
                    [
                        pa.array(expected["reason"]),
                        pa.array(expected["flags"]),
                        pa.array(values, type=pa.string()),
                        pa.array(codes, type=pa.uint32()),
                        pa.array(language_overrides, type=pa.string()),
                    ],
                    schema=schema,
                )

            branch.add_columns(
                selection_columns,
                read_columns=["sample_id", "text", "language", "_rowaddr"],
                batch_size=65536,
            )
    if branch.count_rows() != source["rows"] or not branch_files(ds) <= branch_files(branch):
        raise ValueError("Branch changed base rows or file references")
    reasons, lang_rows, lang_seconds = Counter(), Counter(), Counter()
    selected_duration = 0.0
    ordered = hashlib.sha256()
    position = 0
    for batch in branch.to_batches(
        columns=[
            "sample_id",
            "text",
            "selection_reason",
            "selection_flags",
            "selected_text",
            "selected_text_source",
            "language",
            "selected_language",
        ],
        batch_size=65536,
        scan_in_order=True,
    ):
        actual_ids = np.asarray(batch.column("sample_id").to_pylist(), dtype="S64")
        expected = array[position : position + len(batch)]
        actual_reasons = batch.column("selection_reason").to_numpy()
        actual_flags = batch.column("selection_flags").to_numpy()
        if not (
            np.array_equal(actual_ids, expected["sample_id"])
            and np.array_equal(actual_reasons, expected["reason"])
            and np.array_equal(actual_flags, expected["flags"])
        ):
            raise ValueError("Published branch differs from audited decisions")
        texts = batch.column("text").to_pylist()
        values, codes = overrides(texts, list(range(position, position + len(batch))), asr)
        if (
            batch.column("selected_text").to_pylist() != values
            or batch.column("selected_text_source").to_pylist() != codes
        ):
            raise ValueError("Selected text does not match normalization rule")
        hashes = [
            hashlib.sha256((v if v is not None else (t or "")).encode()).digest()
            for v, t in zip(values, texts, strict=True)
        ]
        # Compare as S32: numpy drops trailing NUL bytes when an element is read back as bytes.
        if not np.array_equal(np.array(hashes, dtype="S32"), expected["text_hash"]):
            raise ValueError("Published selected text differs from audited text")
        aliases = p["rules"]["language_aliases"]
        languages = batch.column("language").to_pylist()
        expected_languages = [
            aliases[lang] if lang in aliases and aliases[lang] != lang else None
            for lang in languages
        ]
        if batch.column("selected_language").to_pylist() != expected_languages:
            raise ValueError("Selected language does not match alias rule")
        ordered.update(b"".join(s + b"\n" for s in actual_ids))
        codes, counts = np.unique(actual_reasons, return_counts=True)
        reasons.update({str(int(c)): int(n) for c, n in zip(codes, counts, strict=True)})
        selected = expected["reason"] == 0
        selected_duration += float(expected["duration"][selected].sum())
        for code, language in enumerate(p["rules"]["supported_languages"], 1):
            mask = selected & (expected["language"] == code)
            lang_rows[language] += int(mask.sum())
            lang_seconds[language] += float(expected["duration"][mask].sum())
        position += len(batch)
    if position != source["rows"] or "65535" in reasons:
        raise ValueError("Incomplete branch")
    if lance.dataset(Path(p["root"]) / source["table_path"]).version != source["lance_version"]:
        raise ValueError("Main changed during selection publication")
    base_tag = f"base-{source['release_id']}-v{source['lance_version']}"
    for tag, reference in [
        (base_tag, (None, source["lance_version"])),
        (p["selection_id"], (branch_name, branch.version)),
    ]:
        tags = ds.tags.list()
        if tag not in tags:
            ds.tags.create(tag, reference)
        else:
            actual = tags[tag]
            if actual["version"] != reference[1] or actual.get("branch") != reference[0]:
                raise ValueError("Existing retention tag has a different target")
    output = {
        "dataset_id": name,
        "release_id": source["release_id"],
        "table_path": source["table_path"],
        "base_version": source["lance_version"],
        "branch": branch_name,
        "lance_version": branch.version,
        "tag": p["selection_id"],
        "rows": position,
        "selected_rows": reasons.get("0", 0),
        "reason_counts": dict(reasons),
        "selected_duration_seconds": selected_duration,
        "selected_language_rows": dict(lang_rows),
        "selected_language_seconds": dict(lang_seconds),
        "validation": {
            "rows_checked": position,
            "null_reason": 0,
            "null_flags": 0,
            "unknown_reason": 0,
            "unknown_flag_bits": 0,
            "pending": 0,
            "base_unchanged": True,
            "ordered_sample_ids_sha256": ordered.hexdigest(),
        },
    }
    write_json(state, output)
    return output


def validate_output_bindings(plan, outputs):
    expected = {(s["dataset_id"], s["release_id"]): s for s in plan["inputs"]}
    actual = {(o["dataset_id"], o["release_id"]): o for o in outputs}
    if len(actual) != len(outputs) or actual.keys() != expected.keys():
        raise ValueError("Selection must bind exactly one branch for each input dataset")
    for key, output in actual.items():
        source = expected[key]
        if (
            output["table_path"] != source["table_path"]
            or output["base_version"] != source["lance_version"]
            or output["rows"] != source["rows"]
            or output["branch"] != plan["selection_id"]
            or type(output["lance_version"]) is not int
            or output["lance_version"] < 1
        ):
            raise ValueError("Selection branch binding disagrees with its fixed plan")


def text_sources(p):
    sources = {
        "0": {"kind": "base_normalization", "normalization": p["rules"]["text_normalization"]}
    }
    for item in p.get("annotation_inputs", []):
        code = str(item["text_source"])
        rule = p["rules"]["text_sources"][code]
        entry = sources.setdefault(
            code,
            {
                "kind": "annotation",
                "task": item["task"],
                "run_id": item["run_id"],
                "manifest_sha256": item["manifest_sha256"],
                "column": "result.text",
                "normalization": rule["normalization"],
                "unavailable": rule["unavailable"],
                "language_policy": rule["language_policy"],
                "language_labels": rule["language_labels"],
                "datasets": [],
            },
        )
        entry["datasets"].append(item["dataset_id"])
    return sources


def publish(work, workers=16):
    import shutil

    p = load_plan(work)
    result = json.loads((Path(work) / "duplicates.json").read_text())
    root = Path(p["root"])
    parent = root / "selections"
    parent.mkdir(exist_ok=True)
    final = parent / p["selection_id"]
    incomplete = parent / (p["selection_id"] + ".incomplete")
    if final.exists():
        raise ValueError("Selection already published; never overwrite")
    owner = incomplete / "execution.json"
    try:
        incomplete.mkdir()
    except FileExistsError:
        if not owner.exists() or json.loads(owner.read_text())["plan_sha256"] != digest(p):
            raise ValueError(
                "Selection ID is reserved by another or unidentified execution"
            ) from None
    else:
        write_json(owner, {"plan_sha256": digest(p)})
    event(work, "publishing_branches", datasets=len(p["inputs"]))
    outputs = []
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        jobs = [
            pool.submit(publish_branch, (p, source, array))
            for source, array in zip(p["inputs"], result["arrays"], strict=True)
        ]
        for job in as_completed(jobs):
            outputs.append(job.result())
            event(
                work,
                "branch_validated",
                dataset=outputs[-1]["dataset_id"],
                datasets_done=len(outputs),
                selected_rows=outputs[-1]["selected_rows"],
            )
    validate_output_bindings(p, outputs)
    # Copy a self-contained scratch result table, never a shallow clone of base.
    destination = incomplete / "duplicates.lance"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(result["duplicate_path"], destination)
    duplicate_table = lance.dataset(destination, version=result["lance_version"])
    if duplicate_table.count_rows() != result["members"]:
        raise ValueError("Copied duplicate table count mismatch")
    for source_file in Path(result["duplicate_path"]).rglob("*"):
        if source_file.is_file() and file_sha(source_file) != file_sha(
            destination / source_file.relative_to(result["duplicate_path"])
        ):
            raise ValueError("Copied duplicate table file mismatch")
    duplicate_table.tags.create(p["selection_id"], (None, result["lance_version"]))
    write_json(incomplete / "rules.json", p["rules"])
    exclusions = incomplete / "exclusions.jsonl"
    exclusions.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in p["exclusions"])
    )
    changes = incomplete / "exclusion_changes.jsonl"
    parent = p.get("parent")
    added = set(p.get("exclusion_additions", [])) if parent else None
    changes.write_text(
        "".join(
            json.dumps(
                {
                    "action": "add",
                    "exclusion_id": e["exclusion_id"],
                    "reason": "initial reviewed evidence"
                    if parent is None
                    else "pinned source timeline evidence",
                }
            )
            + "\n"
            for e in p["exclusions"]
            if added is None or e["exclusion_id"] in added
        )
    )
    parts = json.loads((Path(work) / "scan.json").read_text())
    manifest = {
        "contract_version": "v0.1",
        "artifact_kind": "selection",
        "status": "complete",
        "selection_id": p["selection_id"],
        "purpose": "supervised_tts",
        "finished_at": now(),
        "code_migration": p.get("code_migration"),
        "inputs": p["inputs"],
        "outputs": sorted(outputs, key=lambda o: o["dataset_id"]),
        "annotation_inputs": p.get("annotation_inputs", []),
        "text_sources": text_sources(p),
        "rules": {
            "path": "rules.json",
            "sha256": file_sha(incomplete / "rules.json"),
            "implementation_sha256": p["implementation_sha256"],
        },
        # Version 2 registers 105/1003-1008/2003/2004 and flag bits 10-12; earlier codes keep meaning.
        "reason_dictionary": {
            "version": 2,
            "codes": REASONS,
            "priority": p["rules"]["reason_priority"],
        },
        "flag_dictionary": {"version": 2, "bits": dict(enumerate(FLAGS))},
        "exclusions": {
            "parent": parent,
            "initial_evidence_sources": p["rules"]["initial_exclusion_evidence"],
            "effective_path": exclusions.name,
            "effective_sha256": file_sha(exclusions),
            "changes_path": changes.name,
            "changes_sha256": file_sha(changes),
            "added": len(p["exclusions"]) if parent is None else len(added),
            "retracted": 0,
            "effective_count": len(p["exclusions"]),
        },
        "duplicates": {k: v for k, v in result.items() if k not in {"arrays", "duplicate_path"}},
        "coverage": {
            "row_rules": sum(s["rows"] for s in p["inputs"]),
            "exact_duplicate_hashes": sum(s["rows"] for s in p["inputs"]),
            "full_audio_decode": 0,
            "near_duplicates": "not_checked",
            "quality_scores": "not_checked",
            "audio_statistics": "not_used",
            "audio_presence": p["rules"]["audio_presence_evidence"],
            "text_filter": p["rules"].get("text_filter", "not_applied"),
            "checks": sum((Counter(t.get("checks", {})) for t in parts), Counter()),
        },
        "normalization_statistics": parts,
        "evaluation": {"status": "not_assigned"},
    }
    # Scratch names and hashes are operational details, not consumption dependencies.
    manifest["normalization_statistics"] = [
        {k: v for k, v in t.items() if k not in {"file", "sha256", "fragments"}} for t in parts
    ]
    manifest["duplicates"].update(
        table_path="duplicates.lance",
        branch=None,
        schema_sha256=digest(schema_description(duplicate_schema())),
        rows=result["members"],
        comparison="full_audio_sha256",
        index_columns=["sample_id", "audio_sha256"],
    )
    for source in p["inputs"]:
        open_base(root, source)
    write_json(incomplete / "manifest.json", manifest)
    os.rename(incomplete, final)
    event(
        work,
        "complete",
        selection_path=str(final),
        selected_rows=sum(o["selected_rows"] for o in outputs),
        selected_hours=sum(o["selected_duration_seconds"] for o in outputs) / 3600,
    )
    return manifest
