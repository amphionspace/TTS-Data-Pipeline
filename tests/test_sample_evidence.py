import csv
import hashlib

import pytest

from tts_data_pipeline import selection
from tts_data_pipeline.selection import classify

from test_selection_filter import asr, row, rules


def test_rows_outside_the_sample_list_are_not_sampled():
    base = row("kept")
    annotation = asr(base)
    with_code = rules() | {"reason_priority": [8002, *rules()["reason_priority"]]}
    reason, flags, _, _ = classify(base, "alpha", with_code, set(), {}, annotation, sampled={"b" * 64})
    assert reason == 8002 and flags & (1 << selection.SAMPLE_BIT)
    reason, _, _, _ = classify(base, "alpha", with_code, set(), {}, annotation, sampled={base["sample_id"]})
    assert reason == 0
    reason, _, _, _ = classify(base, "alpha", with_code, set(), {}, annotation, sampled=None)
    assert reason == 0


def test_sample_evidence_is_pinned(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "reports").mkdir(parents=True)
    tsv = repo / "reports" / "sample.tsv"
    with tsv.open("w", newline="") as stream:
        w = csv.writer(stream, delimiter="\t", lineterminator="\n")
        w.writerow(["dataset", "sample_id", "speaker_id", "duration_seconds"])
        w.writerow(["alpha", "a" * 64, "s1", "3.0"])
        w.writerow(["alpha", "b" * 64, "s2", "4.0"])
    sha = hashlib.sha256(tsv.read_bytes()).hexdigest()
    monkeypatch.setattr(selection, "__file__", str(repo / "src" / "pkg" / "selection.py"))
    item = dict(dataset_id="alpha", path="reports/sample.tsv", sha256=sha, expected_rows=2)
    inputs = [{"dataset_id": "alpha", "manifest_sha256": "m"}]
    (pinned,) = selection.sample_evidence({"sample_evidence": [item]}, inputs)
    assert pinned["base_manifest_sha256"] == "m"
    assert selection.sample_list({"sample_evidence": [pinned]}, "alpha") == {"a" * 64, "b" * 64}
    assert selection.sample_list({"sample_evidence": [pinned]}, "beta") is None
    with pytest.raises(ValueError, match="scope changed"):
        selection.sample_evidence({"sample_evidence": [item | {"expected_rows": 3}]}, inputs)
    with pytest.raises(ValueError, match="changed"):
        selection.sample_evidence({"sample_evidence": [item | {"sha256": "0" * 64}]}, inputs)
