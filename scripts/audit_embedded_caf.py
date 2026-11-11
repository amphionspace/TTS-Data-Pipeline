"""Find WAV records whose data chunk starts with an embedded Apple CAF file header.

Such a header (``caff`` ... ``desc``/``lpcm``/``free``/``data``) is decoded as PCM by every WAV
reader: a loud burst at time 0 and another near the CAF ``data`` tag, before the real audio.
Writes one TSV row per affected record and a summary JSON; selection pins the TSV as
``audio_content_evidence`` (reason 103, audio_sha256 scope).

    python scripts/audit_embedded_caf.py --root /workspace/data/DATA-TTS-UNIFIED \
        --dataset flexivoice --out reports/embedded-caf
"""

import argparse
import csv
import json
import struct
import time
from collections import Counter
from pathlib import Path

import lance


def wav_data_offset(raw):
    """Byte offset of the RIFF/WAVE data chunk payload, or None."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(raw):
        tag, size = raw[pos : pos + 4], struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        if tag == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def embedded_caf(raw):
    """(wav_data_offset, caf_pcm_offset) when the WAV payload is a CAF file, else None."""
    start = wav_data_offset(raw)
    if start is None or raw[start : start + 4] != b"caff":
        return None
    pos = start + 8  # 'caff', uint16 version, uint16 flags
    while pos + 12 <= len(raw):
        tag, size = raw[pos : pos + 4], struct.unpack(">q", raw[pos + 4 : pos + 12])[0]
        if tag == b"data":
            return start, pos + 12 + 4  # chunk header, then uint32 edit count
        if size < 0:
            break
        pos += 12 + size
    raise ValueError("CAF header without a data chunk")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = args.root / "datasets" / args.dataset / "v0.1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    table = manifest_path.parent / manifest["table_path"]
    ds = lance.dataset(table, version=manifest["lance_version"])
    args.out.mkdir(parents=True, exist_ok=True)
    tsv = args.out / f"{args.dataset}-findings.tsv"
    columns = ["sample_id", "audio_sha256", "sample_rate", "audio"]
    has_config = "source_config" in ds.schema.names
    if has_config:
        columns.append("source_config")
    total, hits, headers = Counter(), Counter(), Counter()
    started, rows = time.time(), 0
    with tsv.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["dataset", "sample_id", "audio_sha256", "source_config", "sample_rate",
             "wav_data_offset", "caf_pcm_offset", "header_bytes"]
        )
        for batch in ds.to_batches(columns=columns, batch_size=2048):
            audio = batch["audio"].field("bytes").to_pylist()
            config = batch["source_config"].to_pylist() if has_config else None
            for i, (sid, sha, rate) in enumerate(
                zip(*(batch[c].to_pylist() for c in ("sample_id", "audio_sha256", "sample_rate")))
            ):
                name = config[i] if config else ""
                total[name] += 1
                found = embedded_caf(audio[i])
                if found:
                    start, pcm = found
                    hits[name] += 1
                    headers[pcm - start] += 1
                    writer.writerow([args.dataset, sid, sha, name, rate, start, pcm, pcm - start])
            rows += batch.num_rows
            if rows % (2048 * 100) < 2048:
                print(json.dumps(dict(rows=rows, hits=sum(hits.values()),
                                      seconds=round(time.time() - started))), flush=True)
    if rows != manifest["rows"]:
        raise ValueError("Scan did not cover the published base")
    summary = dict(
        dataset=args.dataset,
        base_manifest_sha256=__import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest(),
        lance_version=manifest["lance_version"],
        rows=rows,
        findings=sum(hits.values()),
        rows_by_source_config=dict(total),
        findings_by_source_config=dict(hits),
        header_bytes=dict(headers),
        tsv=tsv.name,
        tsv_sha256=__import__("hashlib").sha256(tsv.read_bytes()).hexdigest(),
        seconds=round(time.time() - started),
    )
    (args.out / f"{args.dataset}-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
