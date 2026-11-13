"""Deterministic per-speaker sample of a base dataset, written as a TSV for selection.

Rows whose native duration lies in [min_seconds, max_seconds] are ranked within each base
speaker_id by sha256(f"{seed}:{sample_id}"); the first `per_speaker` are listed. Selection pins
the TSV as `sample_evidence` (rows not listed: reason 8002 not_sampled), so the sample is part
of the published data decision rather than a build-time filter.

    python scripts/sample_per_speaker.py --root /workspace/data/DATA-TTS-UNIFIED \
        --dataset hifitts2 --per-speaker 40 --min-seconds 3 --max-seconds 20 \
        --seed hifitts2-speaker40-v1 --out reports/sample/hifitts2-speaker40-v1.tsv
"""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import lance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--per-speaker", type=int, required=True)
    parser.add_argument("--min-seconds", type=float, required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.root / "datasets" / args.dataset / "v0.1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    ds = lance.dataset(manifest_path.parent / manifest["table_path"], version=manifest["lance_version"])
    table = ds.to_table(columns=["sample_id", "speaker_id", "duration_seconds"])
    if table.num_rows != manifest["rows"]:
        raise ValueError("Base snapshot differs from its manifest")
    by = defaultdict(list)
    for sid, speaker, seconds in zip(*(table[c].to_pylist() for c in table.column_names)):
        if speaker and args.min_seconds <= seconds <= args.max_seconds:
            rank = hashlib.sha256(f"{args.seed}:{sid}".encode()).hexdigest()
            by[speaker].append((rank, sid, seconds))
    rows = []
    for speaker in sorted(by):
        for rank, sid, seconds in sorted(by[speaker])[: args.per_speaker]:
            rows.append((sid, speaker, seconds))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(["dataset", "sample_id", "speaker_id", "duration_seconds"])
        for sid, speaker, seconds in sorted(rows):
            writer.writerow([args.dataset, sid, speaker, f"{seconds:.3f}"])
    summary = dict(
        dataset=args.dataset,
        base_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        rule=f"per base speaker_id, {args.min_seconds}-{args.max_seconds} s native duration, "
        f"first {args.per_speaker} by sha256('{args.seed}:' + sample_id)",
        rows=len(rows),
        speakers=len({r[1] for r in rows}),
        hours=round(sum(r[2] for r in rows) / 3600, 2),
        tsv_sha256=hashlib.sha256(args.out.read_bytes()).hexdigest(),
    )
    args.out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
