import json
import threading

import httpx
import lance
import pyarrow as pa
import pytest

from tts_data_pipeline.annotations.speaker_structure import run as diar
from tts_data_pipeline.annotations.speaker_structure.response import InvalidOutput, build_result
from tts_data_pipeline.annotations.speaker_structure.service import Service, ServiceDown
from tts_data_pipeline.feature_runtime import write_json

RULES = dict(end_tolerance_seconds=1.0, truncation_gap_seconds=3.0, truncation_gap_fraction=0.3)
BASE = dict(audio_sha256="a", sample_rate=100, num_frames=1000)  # 10 s at 100 Hz.


def body(*segments):
    return json.dumps(
        dict(
            duration=10.0,
            segments=[dict(id=i, start=s, end=e, text=t) for i, (s, e, t) in enumerate(segments)],
        )
    )


def test_two_speakers_with_overlap():
    r = build_result(
        body((0.0, 5.0, "[S01]hello"), (4.0, 9.5, "[S02]hi"), (9.0, 10.4, "[S01]bye")), BASE, RULES
    )
    assert r["speaker_count"] == 2
    assert [t["speaker"] for t in r["turns"]] == ["S01", "S02", "S01"]
    assert r["turns"][2] == dict(speaker="S01", start_sample=900, end_sample=1000, text="bye")
    assert r["overlaps"] == [
        dict(start_sample=400, end_sample=500),
        dict(start_sample=900, end_sample=950),
    ]
    assert r["overlap_samples"] == 150 and r["overlap_ratio"] == 0.15
    assert r["turn_union_samples"] == 1000
    assert r["dominant_speaker_ratio"] == pytest.approx(600 / 1000)


def test_no_turns_is_ok_without_a_dominant_ratio():
    r = build_result(body(), BASE, RULES)
    assert r["speaker_count"] == 0 and r["turns"] == [] and r["overlap_ratio"] == 0
    assert r["dominant_speaker_ratio"] is None


@pytest.mark.parametrize(
    "response, code",
    [
        ("not json", "model_invalid_output"),
        (json.dumps(dict(segments=None)), "model_invalid_output"),
        (body((0.0, 2.0, "hello")), "model_invalid_output"),
        (body((7.79, 7.79, "[S01]de la editorial")), "model_invalid_output"),
        (body((0.0, 11.5, "[S01]runs past the audio")), "model_invalid_output"),
        (body((0.0, 2.0, "[S01]stops early")), "output_truncated"),
    ],
)
def test_unusable_output(response, code):
    with pytest.raises(InvalidOutput) as error:
        build_result(response, BASE, RULES)
    assert error.value.code == code


def make_profile(**service):
    return dict(
        service=dict(
            base_url="http://moss",
            models_response_sha256=None,
            identity_check_seconds=0,
            outage_wait_seconds=0.01,
            outage_checks=2,
            **service,
        ),
        request=dict(
            form=dict(model="m", response_format="verbose_json"),
            timeout_seconds=5,
            concurrency=4,
            resend_attempts=3,
        ),
        validation=RULES,
    )


def service_with(handler, **kwargs):
    events = []
    profile = make_profile(**kwargs)
    service = Service(profile, lambda phase, **f: events.append(phase))
    service.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.probe = httpx.Client(transport=httpx.MockTransport(handler))
    models = service.client.get("http://moss/v1/models").content
    import hashlib

    profile["service"]["models_response_sha256"] = hashlib.sha256(models).hexdigest()
    return service, events


ROW = dict(sample_id="s", audio_name="x.flac", audio_bytes=b"x", **BASE)


def test_bad_output_is_resent_then_left_unfinished():
    calls = []

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=dict(data=[dict(id="m")]))
        calls.append(1)
        return httpx.Response(200, text=body((0.0, 2.0, "no label")))

    service, _ = service_with(handler)
    status, code, result, raw, attempts = service.request(ROW)
    assert (status, code, result, attempts, len(calls)) == (
        "failed",
        "model_invalid_output",
        None,
        3,
        3,
    )
    assert "no label" in raw


def test_outage_pauses_and_does_not_count_against_the_sample():
    down = threading.Event()
    down.set()
    posts = []

    def handler(request):
        if request.url.path == "/v1/models":
            if down.is_set() and len(posts) == 1:
                down.clear()  # The service comes back after the first check.
                return httpx.Response(503)
            return httpx.Response(200, json=dict(data=[dict(id="m")]))
        posts.append(1)
        if len(posts) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text=body((0.0, 9.8, "[S01]fine")))

    service, events = service_with(handler)
    status, code, result, raw, attempts = service.request(ROW)
    assert status == "ok" and attempts == 1 and result["speaker_count"] == 1
    assert "outage" in events and "service_back" in events


def test_service_that_stays_down_stops_the_stage():
    state = dict(up=True)

    def handler(request):
        if request.url.path == "/v1/models":
            if state["up"]:
                return httpx.Response(200, json=dict(data=[dict(id="m")]))
            raise httpx.ConnectError("refused")
        state["up"] = False
        raise httpx.ConnectError("refused")

    service, events = service_with(handler)
    with pytest.raises(ServiceDown):
        service.request(ROW)
    assert events.count("still_down") == 2


def test_rounds_retry_failures_then_publish(tmp_path, monkeypatch):
    root = tmp_path / "root"
    release = root / "datasets/emilia/v0.1"
    rows = [
        dict(
            sample_id=f"s{i}",
            audio_sha256=f"a{i}",
            audio=dict(bytes=b"x", path=f"s{i}.mp3"),
            sample_rate=100,
            num_frames=1000,
        )
        for i in range(5)
    ]
    ds = lance.write_dataset(
        pa.Table.from_pylist(rows), release / "samples.lance", max_rows_per_file=2
    )
    write_json(
        release / "manifest.json",
        dict(status="complete", rows=5, table_path="samples.lance", lance_version=ds.version),
    )
    config = tmp_path / "server.json"
    write_json(config, dict(model_id="moss", model_revision="r", served_name="m", serving={}))
    state = dict(bad={"s1.mp3", "s3.mp3"})

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=dict(data=[dict(id="m")]))
        name = request.content.split(b'filename="')[1].split(b'"')[0].decode()
        if name in state["bad"]:
            return httpx.Response(200, text="garbled")
        return httpx.Response(200, text=body((0.0, 9.9, "[S01]ok")))

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "get", lambda url, **k: real_client(transport=transport).get(url))
    monkeypatch.setattr(diar.Service, "__init__", _patched_init(Service.__init__, transport))
    diar.prepare(root, tmp_path / "work", "http://moss", config, ("emilia",))
    assert diar.run(tmp_path / "work") == {"ok": 3, "failed": 2}
    state["bad"] = {"s3.mp3"}
    assert diar.run(tmp_path / "work") == {"ok": 4, "failed": 1}
    path = diar.publish(tmp_path / "work")
    manifest = json.loads(path.read_text())
    output = manifest["outputs"][0]
    assert output["coverage"]["ok"] == 4 and output["coverage"]["failed"] == 1
    assert output["error_codes"] == {"model_invalid_output": 1}
    assert len(manifest["execution"]["rounds"]) == 2
    table = lance.dataset(root / output["table"]["table_path"]).to_table()
    assert table["sample_id"].to_pylist() == [f"s{i}" for i in range(5)]
    assert "raw_response" not in table.schema.names


def _patched_init(original, transport):
    def init(self, profile, log):
        original(self, profile, log)
        self.client = httpx.Client(transport=transport)
        self.probe = httpx.Client(transport=transport)

    return init


def test_failed_identity_probe_pauses_instead_of_stopping():
    calls = dict(models=0)

    def handler(request):
        calls["models"] += 1
        if calls["models"] in (2, 3):  # The identity probe and the outage check both fail.
            return httpx.Response(503)
        return httpx.Response(200, json=dict(data=[dict(id="m")]))

    service, events = service_with(handler)
    service.check_identity(force=True)
    assert events == ["outage", "service_back"]
