"""Retry temporary service failures; retain permanent rejection evidence."""

import http.client
import json
import time
import urllib.error

ATTEMPTS = 3
RETRY_DELAYS = (2, 5)
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


class TransientASRError(RuntimeError):
    """The same pinned request can be retried without changing its meaning."""


class RequestRejected(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"ASR HTTP {status}: {body[:2048]}")


def call(operation):
    for attempt in range(ATTEMPTS):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            exc.close()
            if exc.code not in RETRYABLE_HTTP:
                raise RequestRejected(exc.code, body) from exc
            error = TransientASRError(f"ASR HTTP {exc.code}: {body[:2048]}")
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            TransientASRError,
        ) as exc:
            error = TransientASRError(f"{type(exc).__name__}: {exc}")
        if attempt == ATTEMPTS - 1:
            raise error
        delay = RETRY_DELAYS[attempt]
        print(
            json.dumps(
                dict(
                    event="asr_request_retry",
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    error=str(error),
                )
            ),
            flush=True,
        )
        time.sleep(delay)
