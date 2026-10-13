"""Distribution of the upstream DNSMOS of the Emilia family, by dataset, language and duration.

Read-only. Emilia and Emilia-YODAS keep the score at metadata_json.upstream.dnsmos, Emilia2 at
metadata_json.upstream.short.dnsmos (one score per short clip). Writes one JSON row per
(dataset, language, duration bucket, Emilia2 ingest operation) with a 0.01-wide histogram:

    report_upstream_dnsmos.py --out dnsmos_cells.json [--datasets emilia emilia_yodas emilia2]

Used for docs/upstream-dnsmos-report.md.
"""

import argparse
import json
import multiprocessing
import re
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lance
import numpy as np

ROOT = "/workspace/data/DATA-TTS-UNIFIED/datasets/{}/v0.1/samples.lance"
EDGES = [0, 2, 3, 5, 9, 15, 30, float("inf")]
LABELS = ["<2s", "2-3s", "3-5s", "5-9s", "9-15s", "15-30s", ">=30s"]
SCORE = re.compile(r'"dnsmos": ([-0-9.eE]+)')
OPERATION = re.compile(r'"ingestion_operation": "([a-z_]+)"')
COLUMNS = ["language", "duration_seconds", "metadata_json"]


def fragment_cells(args):
    """(histograms, seconds) keyed by cell for one fragment."""
    name, fragment_id = args
    ds = lance.dataset(ROOT.format(name))
    hists, seconds = defaultdict(lambda: np.zeros(501, np.int64)), defaultdict(float)
    for batch in ds.get_fragment(fragment_id).to_batches(columns=COLUMNS, batch_size=65536):
        for language, duration, meta in zip(*(batch.column(c).to_pylist() for c in COLUMNS)):
            score = float(SCORE.search(meta).group(1))
            bucket = LABELS[np.searchsorted(EDGES, duration, side="right") - 1]
            operation = OPERATION.search(meta)
            key = (name, language or "?", bucket, operation.group(1) if operation else "-")
            hists[key][min(500, int(round(score * 100)))] += 1
            seconds[key] += duration
    return dict(hists), dict(seconds)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--datasets", nargs="+", default=["emilia", "emilia_yodas", "emilia2"])
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    jobs = [
        (name, fragment.fragment_id)
        for name in args.datasets
        for fragment in lance.dataset(ROOT.format(name)).get_fragments()
    ]
    hists, seconds = defaultdict(lambda: np.zeros(501, np.int64)), defaultdict(float)
    # Lance is not fork-safe; spawned workers open the datasets themselves.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(args.workers, mp_context=context) as pool:
        for part_hists, part_seconds in pool.map(fragment_cells, jobs, chunksize=4):
            for key, hist in part_hists.items():
                hists[key] += hist
            for key, value in part_seconds.items():
                seconds[key] += value
    rows = [
        dict(
            dataset=name,
            language=language,
            duration=bucket,
            operation=operation,
            rows=int(hist.sum()),
            hours=seconds[(name, language, bucket, operation)] / 3600,
            hist=hist.tolist(),
        )
        for (name, language, bucket, operation), hist in sorted(hists.items())
    ]
    args.out.write_text(json.dumps(rows) + "\n")


if __name__ == "__main__":
    main()
