import copy
import runpy
import threading
from pathlib import Path

import pytest

from tts_data_pipeline.annotations.speaker_structure import run as diar
from tts_data_pipeline.annotations.speaker_structure.service import ModelChanged, ServiceDown


@pytest.fixture
def runtime(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    entry = runpy.run_path(str(scripts / "annotate_speaker_structure.py"))
    monkeypatch.setattr(diar, "Service", diar.Service)
    clock = [0.0]
    monkeypatch.setattr(entry["time"], "monotonic", lambda: clock[0])
    monkeypatch.setattr(entry["time"], "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    entry["override_endpoint"]("http://new", 60, 240)
    profile = dict(
        service=dict(base_url="http://old", models_response_sha256="same",
                     identity_check_seconds=600, outage_wait_seconds=1800, outage_checks=8),
        request=dict(timeout_seconds=600, concurrency=256),
    )
    original = copy.deepcopy(profile)
    events = []
    service = diar.Service(profile, lambda phase, **fields: events.append((phase, fields)))
    assert profile == original
    try:
        yield service, clock, events
    finally:
        service.client.close()
        service.probe.close()


def test_runtime_outage_recovers_at_next_minute(runtime, monkeypatch):
    service, clock, events = runtime
    calls = []

    def probe():
        calls.append(clock[0])
        if len(calls) == 1:
            raise OSError("offline")
        return "same"

    monkeypatch.setattr(service, "models_digest", probe)
    assert service.outage() is True
    assert calls == [0, 60]
    assert service.healthy.is_set()
    assert events[-1][0] == "service_back"


def test_runtime_deadline_includes_probe_duration(runtime, monkeypatch):
    service, clock, events = runtime
    calls = []

    def probe():
        calls.append(clock[0])
        if service.outage_deadline is not None:
            clock[0] += 50
        raise OSError("offline")

    monkeypatch.setattr(service, "models_digest", probe)
    with pytest.raises(ServiceDown, match="240s"):
        service.outage()
    assert clock[0] == 240
    assert calls == [0, 60, 170]
    assert isinstance(service.dead, ServiceDown)


def test_runtime_recovery_still_rejects_model_change(runtime, monkeypatch):
    service, clock, events = runtime

    def probe():
        if clock[0] == 0:
            raise OSError("offline")
        return "different-model"

    monkeypatch.setattr(service, "models_digest", probe)
    with pytest.raises(ModelChanged):
        service.outage()
    assert "service_back" not in [phase for phase, _ in events]


def test_thread_clients_keep_one_client_per_thread(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    entry = runpy.run_path(str(scripts / "annotate_speaker_structure.py"))
    httpx = entry["httpx"]
    real = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=request.url.path))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real(transport=transport, **kwargs))
    clients, results = entry["ThreadClients"](5), []

    def work():
        results.extend(clients.post(f"http://moss/{p}").text for p in "ab")

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["/a"] * 3 + ["/b"] * 3
    assert len(clients.clients) == 3
    clients.close()
