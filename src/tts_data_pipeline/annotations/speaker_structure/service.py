"""MOSS service access: one request per sample, resend rules, and outage pauses (spec 05).

A sample's request is resent on a timeout, a server error while the service is healthy, an
unusable response or suspected truncation, up to `resend_attempts`; then it stays unfinished
for this round. When the health check fails, every worker pauses; the check repeats every
`outage_wait_seconds` up to `outage_checks` times before the stage stops. Time spent while
the service is down never counts against a sample. Health and identity probes use their own
connection, so a stalled request pool cannot make the service look down or stop the run.
"""

import hashlib
import threading
import time

import httpx

from .response import InvalidOutput, build_result

REJECTED_HTTP = {400, 413, 415, 422}


class ServiceDown(RuntimeError):
    """The service stayed unavailable through every outage check: stop the stage."""


class ModelChanged(RuntimeError):
    """The service's model identity differs from the pinned profile: stop the stage."""


class Service:
    def __init__(self, profile, log):
        self.p, self.log = profile, log
        self.client = httpx.Client(
            timeout=profile["request"]["timeout_seconds"],
            limits=httpx.Limits(max_connections=profile["request"]["concurrency"]),
        )
        self.probe = httpx.Client(timeout=60)
        self.lock, self.healthy = threading.Lock(), threading.Event()
        self.healthy.set()
        self.dead = None  # Set when the stage must stop; wakes every waiting worker.
        self.last_identity_check = 0.0

    def models_digest(self):
        r = self.probe.get(self.p["service"]["base_url"] + "/v1/models")
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()

    def verify(self, digest):
        if digest != self.p["service"]["models_response_sha256"]:
            self.stop(ModelChanged(f"/v1/models changed: {digest}"))
        self.last_identity_check = time.time()

    def check_identity(self, force=False):
        """Stop on a different model; a failed probe pauses like any outage, then retries."""
        interval = self.p["service"]["identity_check_seconds"]
        if not force and time.time() - self.last_identity_check < interval:
            return
        while True:
            self.wait_healthy()
            try:
                digest = self.models_digest()
            except httpx.HTTPError:
                self.outage()
                continue
            self.verify(digest)
            return

    def outage(self):
        """After a transport or server error: True if the service was down (and is back now),
        False if it answers, so the error belongs to this request and counts as an attempt."""
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
        wait, checks = self.p["service"]["outage_wait_seconds"], self.p["service"]["outage_checks"]
        for check in range(1, checks + 1):
            time.sleep(wait)
            try:
                digest = self.models_digest()
            except Exception as error:
                self.log("still_down", check=check, of=checks, error=str(error)[:300])
                continue
            self.verify(digest)
            self.log("service_back", check=check)
            self.healthy.set()
            return True
        self.stop(ServiceDown(f"service unavailable after {checks} checks every {wait}s"))

    def stop(self, error):
        self.dead = error
        self.healthy.set()
        raise error

    def wait_healthy(self):
        self.healthy.wait()
        if self.dead is not None:
            raise self.dead

    def request(self, row):
        """(status, error_code, result, raw_response, attempts) for one base row."""
        rules, attempts, last = self.p["validation"], 0, None
        while attempts < self.p["request"]["resend_attempts"]:
            self.wait_healthy()
            try:
                r = self.client.post(
                    self.p["service"]["base_url"] + "/v1/audio/transcriptions",
                    files={"file": (row["audio_name"], row["audio_bytes"])},
                    data=self.p["request"]["form"],
                )
            except (httpx.TransportError, httpx.TimeoutException) as error:
                if not self.outage():
                    attempts += 1
                    timeout = isinstance(error, httpx.TimeoutException)
                    last = ("failed", "request_timeout" if timeout else "transport_error", None)
                continue
            if r.status_code >= 500 or r.status_code == 429:
                if not self.outage():
                    attempts += 1
                    last = ("failed", "server_error", r.text)
                continue
            attempts += 1
            if r.status_code in REJECTED_HTTP:
                last = ("failed", "server_rejected_input", r.text)
                continue
            if r.status_code != 200:
                last = ("failed", f"http_{r.status_code}", r.text)
                continue
            try:
                return "ok", None, build_result(r.text, row, rules), r.text, attempts
            except InvalidOutput as error:
                last = ("failed", error.code, r.text)
        status, code, raw = last
        return status, code, None, raw, attempts
