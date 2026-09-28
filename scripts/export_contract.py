"""Regenerate the machine-readable contract and synthetic base-row example."""

import io
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import soundfile as sf

from tts_data_pipeline.contract import contract_types
from tts_data_pipeline.schema import make_record, source_file_snapshot


def export(root):
    (root / "schemas" / "arrow-schemas.json").write_text(
        json.dumps(contract_types(), ensure_ascii=False, indent=2) + "\n"
    )
    audio = io.BytesIO()
    sf.write(audio, np.zeros(1600), 16000, format="WAV", subtype="PCM_16")
    import hashlib

    data = audio.getvalue()
    record = make_record(
        dataset_id="example_synthetic",
        source_snapshot=source_file_snapshot("tone.wav", hashlib.sha256(data).hexdigest()),
        source_key="tone_001",
        audio_bytes=data,
        audio_name="tone.wav",
        source_locator={"root": "example", "path": "tone.wav"},
        source_split="train",
        metadata={"example_only": True, "asset_kind": "synthetic_tone"},
    )
    record["audio"]["bytes"] = f"<binary omitted: {len(data)} bytes>"
    example = {
        "example_only": True,
        "representation": (
            "JSON projection; bytes redacted; hashes refer to the original synthetic record"
        ),
        "record": record,
    }
    (root / "examples" / "base-row.example.json").write_text(
        json.dumps(example, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    export(Path(__file__).resolve().parents[1] / "docs" / "data-contract")
