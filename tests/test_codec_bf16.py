from types import SimpleNamespace

import pytest

from tts_data_pipeline.codec.qwen3_12hz.encoder import Encoder
from tts_data_pipeline.codec.qwen3_12hz.run import prepare, write_json


def test_guard_audit_does_not_grow_with_batches():
    encoder = object.__new__(Encoder)
    history = []

    def encode(waves):
        history.append({"samples": len(waves), "rechecked": [0], "native_stages": 3})
        return ["codes"] * len(waves)

    encoder.runner = SimpleNamespace(encode_many=encode, guard_history=history)
    encoder.audit = {"samples": 0, "rechecked_samples": 0, "native_quantizer_stages": 0}
    for _ in range(100):
        assert encoder.encode_many([1, 2]) == ["codes", "codes"]
        assert not history
    assert encoder.audit == {
        "samples": 200,
        "rechecked_samples": 100,
        "native_quantizer_stages": 300,
    }


def test_packed_batch_limit_precedes_gpu_execution():
    encoder = object.__new__(Encoder)
    with pytest.raises(ValueError, match="at most 64"):
        encoder.encode_many([None] * 65)


def test_bf16_planning_rejects_legacy_fp16_acceptance(tmp_path):
    selection = tmp_path / "selection.json"
    acceptance = tmp_path / "acceptance.json"
    write_json(selection, {"status": "complete"})
    write_json(acceptance, {"canonical_fp16_fa2_accepted": True})
    with pytest.raises(ValueError, match="acceptance"):
        prepare(tmp_path, selection, tmp_path / "work", tmp_path / "models", acceptance)
