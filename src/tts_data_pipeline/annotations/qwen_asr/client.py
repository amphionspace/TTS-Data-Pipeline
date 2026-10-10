"""One real audio per request, with bounded retries for temporary transport failures."""

import json
import re
import urllib.request
import uuid

from . import transport
from .languages import LANGUAGE_NAMES


def request_json(url, payload=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    def read():
        with opener.open(request, timeout=600) as response:
            return json.load(response)

    return transport.call(read)


def check_service(endpoint, model, expected_version):
    models = request_json(endpoint + "/models")
    matches = [m for m in models["data"] if m["id"] == model]
    if len(matches) != 1:
        raise RuntimeError("Pinned ASR model unavailable")
    version = request_json(endpoint.removesuffix("/v1") + "/version")
    if version["version"] != expected_version:
        raise RuntimeError("ASR server version changed")
    return matches[0]


def transcribe(endpoint, model, audio, language=None):
    fields = {
        "model": model,
        "temperature": "0",
        "seed": "0",
        "max_completion_tokens": "1024",
        "stream": "true",
        "stream_include_usage": "true",
    }
    if language:
        # vLLM 0.18.0 Qwen uses to_language to append the official language prefix;
        # its language argument is accepted but ignored by get_generation_prompt.
        fields["to_language"] = language
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"'
        "\r\nContent-Type: audio/wav\r\n\r\n".encode()
    )
    body.extend(audio)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        endpoint + "/audio/transcriptions",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def read():
        with opener.open(request, timeout=600) as stream:
            return read_stream(stream)

    response = transport.call(read)
    if "length" in response["finish_reasons"]:
        return {"error_code": "asr_output_truncated", "raw_response": response}
    if any(reason != "stop" for reason in response["finish_reasons"]):
        raise RuntimeError("Unexpected ASR completion format")
    return dict(
        parse_content(response["content"], language), raw_response=response, error_code=None
    )


def parse_content(raw, requested_language=None):
    # Qwen's silence sentinel is a valid empty transcript, not a network failure.
    if raw.strip() in {"", "<|nospeech|>"}:
        language, text = "", ""
    elif "<asr_text>" in raw and raw.startswith("language "):
        # vLLM emits another prefix for every server-side audio chunk.
        parts = re.split(r"language ([^<\n]+)<asr_text>", raw)
        languages = [lang for lang in parts[1::2] if lang != "None"]
        language = ",".join(dict.fromkeys(languages))
        text = "\n".join(parts[2::2])
    elif requested_language:
        language = LANGUAGE_NAMES[requested_language]
        text = raw
    else:
        raise RuntimeError(f"Unexpected ASR protocol: {raw[:160]!r}")
    return {
        "language": language,
        "language_source": "requested" if requested_language else "detected",
        "text": text,
    }


def read_stream(stream):
    content, finishes, done = [], [], False
    result = {}
    for line in stream:
        if not line.startswith(b"data: "):
            continue
        payload = line[6:].strip()
        if payload == b"[DONE]":
            done = True
            break
        item = json.loads(payload)
        if "error" in item:
            raise RuntimeError(f"ASR stream failed: {item['error']}")
        result.update({k: item[k] for k in ("id", "model", "created", "usage") if k in item})
        for choice in item.get("choices", []):
            content.append(choice.get("delta", {}).get("content") or "")
            if choice.get("finish_reason"):
                finishes.append(choice["finish_reason"])
    if not done or not finishes:
        raise transport.TransientASRError("ASR connection ended before final completion")
    return dict(result, content="".join(content), finish_reasons=finishes)
