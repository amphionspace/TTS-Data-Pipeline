"""MOSS speaker_structure (diarization) of emilia, emilia_yodas and emilia2 via a MOSS service.

    annotate_speaker_structure.py prepare --work W [--base-url http://127.0.0.1:18000]
    annotate_speaker_structure.py run --work W [--base-url URL]  # a new round over every sample not ok yet
    annotate_speaker_structure.py publish --work W    # publishes the last round's results

Datasets run one at a time with the profile's concurrency. A request is resent on timeouts,
unusable output or suspected truncation; service outages pause all requests (checked every
30 minutes, 8 times by default) and never mark samples failed. Runtime outage timing can be
overridden without changing inference settings or checkpoints. Each worker thread keeps its
own connection (see ThreadClients). Spec: data contract 05.
"""

import argparse
import copy
import hashlib
import math
import threading
import time

import _bootstrap  # noqa: F401
import httpx

from tts_data_pipeline.annotations.speaker_structure import run as diar
from tts_data_pipeline.annotations.speaker_structure.service import ServiceDown


class ThreadClients:
    """One keep-alive connection per worker thread, used as `service.client`. httpcore 1.0.9's
    shared pool counts all connections (not idle ones) against max_keepalive on every request:
    with 256 connections that rescan held the GIL for ~10 ms per request, and every finished
    connection was closed, so each request opened a new connection through the tunnel."""

    def __init__(self, timeout):
        self.timeout, self.local = timeout, threading.local()
        self.lock, self.clients = threading.Lock(), []

    def post(self, *args, **kwargs):
        client = getattr(self.local, "client", None)
        if client is None:
            client = self.local.client = httpx.Client(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
            with self.lock:
                self.clients.append(client)
        return client.post(*args, **kwargs)

    def close(self):
        with self.lock:
            for client in self.clients:
                client.close()


def per_thread_connections():
    """Swap the shared request pool for ThreadClients; requests and results are unchanged."""
    pinned = diar.Service

    class Service(pinned):
        def __init__(self, profile, log):
            super().__init__(profile, log)
            self.client.close()
            self.client = ThreadClients(profile["request"]["timeout_seconds"])
            log("transport_override", connections="per_worker_thread")

    diar.Service = Service


def override_endpoint(base_url=None, outage_wait_seconds=None, outage_max_seconds=None):
    """Reach the pinned service at another address for this run only. The endpoint stays out of
    plan.json, so profile_id and checkpoints are unchanged; /v1/models must still match the plan."""
    pinned = diar.Service

    class Service(pinned):
        def __init__(self, profile, log):
            profile = copy.deepcopy(profile)
            self.outage_deadline = None
            if base_url:
                log("endpoint_override", pinned=profile["service"]["base_url"], base_url=base_url)
                profile["service"]["base_url"] = base_url
            if outage_wait_seconds is not None:
                profile["service"]["outage_wait_seconds"] = outage_wait_seconds
                profile["service"]["outage_checks"] = math.ceil(
                    outage_max_seconds / outage_wait_seconds
                )
                log("outage_policy_override", wait_seconds=outage_wait_seconds,
                    max_seconds=outage_max_seconds)
            super().__init__(profile, log)

        def models_digest(self):
            if self.outage_deadline is None:
                return super().models_digest()
            remaining = self.outage_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Outage deadline reached")
            r = self.probe.get(
                self.p["service"]["base_url"] + "/v1/models", timeout=min(60, remaining)
            )
            r.raise_for_status()
            return hashlib.sha256(r.content).hexdigest()

        def outage(self):
            if outage_wait_seconds is None:
                return super().outage()
            with self.lock:
                if not self.healthy.is_set():
                    owner = False
                else:
                    try:
                        self.models_digest()
                        return False
                    except Exception as error:
                        self.healthy.clear()
                        self.log("outage", error=str(error)[:300])
                        owner = True
            if not owner:
                self.wait_healthy()
                return True
            self.outage_deadline = time.monotonic() + outage_max_seconds
            check = 0
            try:
                while time.monotonic() < self.outage_deadline:
                    time.sleep(min(outage_wait_seconds,
                                   max(0, self.outage_deadline - time.monotonic())))
                    if time.monotonic() >= self.outage_deadline:
                        break
                    check += 1
                    try:
                        value = self.models_digest()
                    except Exception as error:
                        self.log("still_down", check=check,
                                 remaining_seconds=max(0, self.outage_deadline - time.monotonic()),
                                 error=str(error)[:300])
                        continue
                    self.verify(value)
                    self.log("service_back", check=check)
                    self.healthy.set()
                    return True
                self.stop(ServiceDown(f"service unavailable for {outage_max_seconds}s"))
            finally:
                self.outage_deadline = None

    diar.Service = Service


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", default="/workspace/data/DATA-TTS-UNIFIED")
    p.add_argument("--work", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:18000")
    p.add_argument("--server-config", default="configs/moss-transcribe-diarize-v1.server.json")
    p.add_argument("--datasets", nargs="+", default=list(diar.DATASETS))
    for name in ("run", "publish"):
        sub.add_parser(name).add_argument("--work", required=True)
    sub.choices["run"].add_argument("--base-url")
    sub.choices["run"].add_argument("--outage-wait-seconds", type=int)
    sub.choices["run"].add_argument("--outage-max-seconds", type=int)
    args = parser.parse_args()
    if args.command == "prepare":
        plan = diar.prepare(
            args.root, args.work, args.base_url, args.server_config, tuple(args.datasets)
        )
        print(plan["run_id"])
    elif args.command == "run":
        timing = (args.outage_wait_seconds, args.outage_max_seconds)
        if any(v is not None for v in timing):
            if any(v is None or v <= 0 for v in timing):
                parser.error("Supply both positive outage wait and maximum seconds")
            if args.outage_wait_seconds >= args.outage_max_seconds:
                parser.error("Outage wait must be shorter than maximum outage duration")
        per_thread_connections()
        if args.base_url or args.outage_wait_seconds is not None:
            override_endpoint(args.base_url.rstrip("/") if args.base_url else None, *timing)
        print(diar.run(args.work))
    else:
        print(diar.publish(args.work))


if __name__ == "__main__":
    main()
