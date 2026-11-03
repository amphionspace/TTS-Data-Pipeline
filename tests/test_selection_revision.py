"""Text-revision text sources (nv_tag_text) and the dataset-scoped sample-rate rule (3003)."""

import json
from pathlib import Path

import lance
import pyarrow as pa
from test_selection_filter import row, rules, write_base, write_parent

from tts_data_pipeline.schema import digest
from tts_data_pipeline.selection import plan, publish, resolve_duplicates, scan, write_json


def write_revision(root, run_id, base, records, results):
    """results: (status, action, text) per record."""
    path = Path("datasets") / base["dataset_id"] / "v0.1/annotations/nv_tag_text" / run_id
    rows = [
        {
            "sample_id": r["sample_id"],
            "input_fingerprint": "x",
            "status": status,
            "error_code": None if status == "ok" else "span_only_annotation",
            "result": {
                "action": action,
                "text": text,
                "audio_sha256": r["audio_sha256"],
                "source_language": r["language"],
            }
            if status == "ok"
            else None,
        }
        for r, (status, action, text) in reversed(list(zip(records, results, strict=True)))
    ]
    ds = lance.write_dataset(pa.Table.from_pylist(rows), root / path / "results.lance")
    folder = root / "annotations/nv_tag_text" / run_id
    folder.mkdir(parents=True)
    fields = [{"name": k} for k in ("action", "text", "audio_sha256", "source_language")]
    write_json(
        folder / "manifest.json",
        {
            "status": "complete",
            "run_id": run_id,
            "profile_id": "nv-canonical-v1",
            "inputs": [base],
            "outputs": [
                {
                    "dataset_id": base["dataset_id"],
                    "table": {
                        "table_path": str(path / "results.lance"),
                        "branch": None,
                        "lance_version": ds.version,
                        "rows": len(rows),
                        "schema": [{"name": "result", "fields": fields}],
                    },
                }
            ],
        },
    )


def revision_rules():
    configuration = rules()
    configuration.update(
        parent_selection="parent",
        source_priority=["alpha"],
        annotation_inputs=[
            {"text_source": 1, "task": "nv_tag_text", "run_id": "rev-run", "datasets": ["alpha"]}
        ],
        text_sources={
            "1": {
                "kind": "text_revision",
                "normalization": "python.str.strip",
                "unavailable": "status != ok excludes (1003); no fallback to base text",
            }
        },
        sample_rate_policy={"alpha": {"min_inclusive": 44100}},
    )
    for key in ("source_timeline_evidence", "text_filter"):
        configuration.pop(key)
    configuration["evaluation"]["held_out_splits"]["values"].append("spk_test")
    priority = configuration["reason_priority"]
    priority.insert(priority.index(3002) + 1, 3003)
    return configuration


def test_revision_text_is_selected_and_low_rate_rows_are_excluded(tmp_path):
    root = tmp_path / "unified"
    wide = {"sample_rate": 48000, "num_frames": 96000}
    records = [
        row("replaced", text="好<crying>的", language="zh") | wide,
        row("kept", text="[Laughter]你好", language="zh") | wide,
        row("span", text="<crying/>别走</crying>", language="zh") | wide,
        row("narrow", text="ok <laughter>", language="zh"),
        row("test", text="测<cough>试", language="zh")
        | wide
        | {"metadata_json": json.dumps({"original_split": "spk_test"})},
    ]
    base = write_base(root, "alpha", records)
    results = [
        ("ok", "replace", "好[Crying]的"),
        ("ok", "keep", None),
        ("skipped", None, None),
        ("ok", "replace", "ok [Laughter]"),
        ("ok", "replace", "测[Cough]试"),
    ]
    write_revision(root, "rev-run", base, records, results)
    write_parent(root, [])
    rules_path = tmp_path / "rules.json"
    write_json(rules_path, revision_rules())
    work = tmp_path / "scratch"
    p = plan(root, work, rules_path, selection_id="nv")
    assert p["annotation_inputs"][0]["kind"] == "text_revision"
    scan(work, workers=2)
    resolve_duplicates(work)
    manifest = json.loads(json.dumps(publish(work, workers=2)))
    (output,) = manifest["outputs"]
    ds = lance.dataset(root / output["table_path"], version=output["base_version"])
    branch = ds.checkout_version((output["branch"], output["lance_version"]))
    got = {
        r["sample_id"]: (r["selection_reason"], r["selected_text"], r["selected_text_source"])
        for r in branch.to_table(
            columns=["sample_id", "selection_reason", "selected_text", "selected_text_source"]
        ).to_pylist()
    }
    assert got == {
        digest("replaced"): (0, "好[Crying]的", 1),
        digest("kept"): (0, None, None),  # keep: base text is the selected text
        digest("span"): (1003, "", 1),  # unavailable: empty override, as for ASR
        digest("narrow"): (3003, "ok [Laughter]", 1),
        digest("test"): (7001, "测[Cough]试", 1),
    }
    assert manifest["reason_dictionary"]["version"] == 7
    assert manifest["reason_dictionary"]["codes"]["3003"] == "below_sample_rate_policy"
    assert manifest["flag_dictionary"]["bits"]["16"] == "sample_rate_policy"
    assert manifest["text_sources"]["1"]["kind"] == "annotation_text_revision"
