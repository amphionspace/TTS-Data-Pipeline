import json

import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.annotations.upstream_quality import run as upstream
from tts_data_pipeline.feature_runtime import write_json

PATH = ["upstream", "dnsmos"]
SHORT = ["upstream", "short", "dnsmos"]


@pytest.mark.parametrize(
    "metadata, path, expected",
    [
        ('{"upstream": {"dnsmos": 3.4671}}', PATH, ("ok", None, 3.4671)),
        ('{"upstream": {"dnsmos": 3}}', PATH, ("ok", None, 3.0)),
        ('{"upstream": {"dnsmos": -0.5}}', PATH, ("ok", None, -0.5)),
        ('{"upstream": {"short": {"dnsmos": 2.8389}}}', SHORT, ("ok", None, 2.8389)),
        ('{"upstream": {"text": "x"}}', PATH, ("skipped", "upstream_missing", None)),
        ('{"upstream": {"dnsmos": null}}', PATH, ("skipped", "upstream_missing", None)),
        ('{"other": 1}', SHORT, ("skipped", "upstream_missing", None)),
        ('{"upstream": {"short": [1]}}', SHORT, ("failed", "upstream_invalid", None)),
        ('{"upstream": {"dnsmos": true}}', PATH, ("failed", "upstream_invalid", None)),
        ('{"upstream": {"dnsmos": "3.1"}}', PATH, ("failed", "upstream_invalid", None)),
        ('{"upstream": {"dnsmos": NaN}}', PATH, ("failed", "upstream_invalid", None)),
        ('{"upstream": {"dnsmos": 1e400}}', PATH, ("failed", "upstream_invalid", None)),
        # orjson parses integers wider than 64 bits as floats: finite, so ok (out of range).
        ('{"upstream": {"dnsmos": 1' + "0" * 30 + "}}", PATH, ("ok", None, 1e30)),
        ('{"upstream": ', PATH, ("failed", "upstream_invalid", None)),
    ],
)
def test_read_score_follows_the_pinned_rule(metadata, path, expected):
    assert upstream.read_score(metadata, path) == expected


def test_fingerprint_binds_the_exact_metadata_bytes():
    base = dict(sample_id="s", audio_sha256="a", metadata_json='{"upstream": {"dnsmos": 3.1}}')
    first = upstream.convert_row("emilia", base, "p")
    same = upstream.convert_row("emilia", dict(base), "p")
    spaced = upstream.convert_row(
        "emilia", dict(base, metadata_json='{"upstream": {"dnsmos":  3.1}}'), "p"
    )
    assert first == same and first["input_fingerprint"] != spaced["input_fingerprint"]
    assert first["result"] == dict(audio_sha256="a", upstream_dnsmos=3.1)


def make_base(root, name, metas):
    release = root / "datasets" / name / "v0.1"
    rows = [
        dict(
            sample_id=f"{name}-{i:04d}",
            audio_sha256=f"audio-{i}",
            language="en",
            metadata_json=json.dumps(m),
        )
        for i, m in enumerate(metas)
    ]
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows), release / "samples.lance", max_rows_per_file=2
    )
    write_json(
        release / "manifest.json",
        dict(
            status="complete", rows=len(rows), table_path="samples.lance", lance_version=ds.version
        ),
    )


def test_prepare_run_publish_and_gate(tmp_path):
    root = tmp_path / "root"
    make_base(root, "emilia", [{"upstream": {"dnsmos": 3.0 + i / 10}} for i in range(5)])
    make_base(
        root, "emilia2", [{"upstream": {"short": {"dnsmos": 2.4 + i / 10}}} for i in range(3)]
    )
    plan = upstream.prepare(root, tmp_path / "work", ("emilia", "emilia2"))
    path = upstream.run(tmp_path / "work", workers=2)
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "complete" and manifest["run_id"] == plan["run_id"]
    by_dataset = {o["dataset_id"]: o for o in manifest["outputs"]}
    assert by_dataset["emilia"]["coverage"]["ok"] == 5
    assert by_dataset["emilia2"]["statistics"]["upstream_dnsmos_min"] == pytest.approx(2.4)
    assert by_dataset["emilia2"]["statistics"]["out_of_expected_range"] == 0
    table = lance.dataset(root / by_dataset["emilia"]["table"]["table_path"]).to_table()
    assert table["sample_id"].to_pylist() == [f"emilia-{i:04d}" for i in range(5)]
    assert [r["upstream_dnsmos"] for r in table["result"].to_pylist()] == pytest.approx(
        [3.0 + i / 10 for i in range(5)]
    )
    assert table["status"].to_pylist() == ["ok"] * 5

    make_base(tmp_path / "root2", "emilia", [{"upstream": {"dnsmos": 3.2}}, {"upstream": {}}])
    upstream.prepare(tmp_path / "root2", tmp_path / "work2", ("emilia",))
    with pytest.raises(ValueError, match="Non-ok rows need review"):
        upstream.run(tmp_path / "work2", workers=1)
    assert not (tmp_path / "root2/annotations").exists()
    path = upstream.run(tmp_path / "work2", workers=1, allow_non_ok=True)
    output = json.loads(path.read_text())["outputs"][0]
    assert output["coverage"]["skipped"] == 1 and output["statistics"]["error_codes"] == {
        "upstream_missing": 1
    }


def test_publish_discards_a_stale_unreceipted_stage(tmp_path):
    root = tmp_path / "root"
    make_base(root, "emilia", [{"upstream": {"dnsmos": 3.1}}, {"upstream": {"dnsmos": 3.2}}])
    plan = upstream.prepare(root, tmp_path / "work", ("emilia",))
    stage = root / f"datasets/emilia/v0.1/annotations/upstream_quality/{plan['run_id']}.incomplete"
    lance.write_dataset(pa.table({"x": [1]}), stage / "results.lance")  # A crashed attempt.
    path = upstream.run(tmp_path / "work", workers=1)
    output = json.loads(path.read_text())["outputs"][0]
    ds = lance.dataset(root / output["table"]["table_path"])
    # A clean publish is one write plus one index commit; stale files would add versions.
    assert [v["version"] for v in ds.versions()] == [1, 2] == [1, output["table"]["lance_version"]]
    assert ds.schema.names == ["sample_id", "input_fingerprint", "status", "error_code", "result"]
