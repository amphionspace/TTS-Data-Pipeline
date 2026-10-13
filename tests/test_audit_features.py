import importlib.util
from pathlib import Path

import numpy as np

from tts_data_pipeline.feature_contract import target_set_sha256

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_features.py"
spec = importlib.util.spec_from_file_location("audit_features", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_member_matches_isin_without_resorting():
    rng = np.random.default_rng(0)
    pool = np.array([f"{i:064x}".encode() for i in rng.integers(0, 10**9, 2000)], dtype="S64")
    excluded = np.unique(pool[:700])
    ids = pool[rng.integers(0, len(pool), 5000)]
    assert np.array_equal(audit.member(ids, excluded), np.isin(ids, excluded))
    assert not audit.member(ids, np.empty(0, dtype="S64")).any()
    # Values past the largest excluded id must not wrap around to a false match.
    tail = np.array([b"f" * 64], dtype="S64")
    assert not audit.member(tail, excluded).any()


def test_sorted_set_hash_matches_the_contract():
    ids = [f"{i:064x}" for i in (5, 1, 3)]
    expected = target_set_sha256(
        [dict(target_kind="sample", target_id=i, parent_sample_id=i) for i in sorted(ids)]
    )
    assert audit.sorted_set_hash(np.asarray(ids, dtype="S64")) == expected
