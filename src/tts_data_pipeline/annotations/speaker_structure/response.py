"""Turn one MOSS verbose_json response into a validated speaker_structure result (spec 05)."""

import hashlib
import json
import math
import re

LABEL = re.compile(r"^\[(S\d+)\]")


class InvalidOutput(ValueError):
    """The response cannot be used as is; the request is resent."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__(f"{code}: {detail}")


def union_length(intervals):
    total, end = 0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def merge(intervals):
    merged = []
    for start, stop in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    return merged


def overlaps(turns):
    """Union of intervals covered by turns of at least two different speakers."""
    pieces = []
    for i, a in enumerate(turns):
        for b in turns[i + 1 :]:
            if a["speaker"] != b["speaker"]:
                start = max(a["start_sample"], b["start_sample"])
                stop = min(a["end_sample"], b["end_sample"])
                if start < stop:
                    pieces.append((start, stop))
    return [dict(start_sample=s, end_sample=e) for s, e in merge(pieces)]


def build_result(body, base, rules):
    """result dict for a 200 response body; raises InvalidOutput for unusable output."""
    raw = body.encode("utf-8")
    try:
        response = json.loads(body)
        segments = response["segments"]
        if not isinstance(segments, list):
            raise TypeError("segments is not a list")
    except (ValueError, KeyError, TypeError) as error:
        raise InvalidOutput("model_invalid_output", f"unparsable response: {error}") from None
    sr, frames = base["sample_rate"], base["num_frames"]
    duration = frames / sr
    tolerance = rules["end_tolerance_seconds"]
    turns = []
    for segment in segments:
        text = segment.get("text") if isinstance(segment, dict) else None
        start, end = (
            (segment.get(k) for k in ("start", "end"))
            if isinstance(segment, dict)
            else (None, None)
        )
        label = LABEL.match(text) if isinstance(text, str) else None
        if label is None:
            raise InvalidOutput(
                "model_invalid_output", f"segment without speaker label: {segment!r:.200}"
            )
        if not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in (start, end)
        ):
            raise InvalidOutput(
                "model_invalid_output", f"segment time not a finite number: {segment!r:.200}"
            )
        if start < 0 or end <= start or end > duration + tolerance:
            raise InvalidOutput(
                "model_invalid_output", f"segment time out of range: {start}-{end} of {duration}"
            )
        start_sample = min(frames, max(0, math.floor(start * sr)))
        end_sample = min(frames, math.ceil(end * sr))
        if start_sample >= end_sample:
            raise InvalidOutput(
                "model_invalid_output", f"empty segment after mapping: {start}-{end}"
            )
        turns.append(
            dict(
                speaker=label.group(1),
                start_sample=start_sample,
                end_sample=end_sample,
                text=text[label.end() :],
            )
        )
    if turns:
        gap = duration - max(t["end_sample"] for t in turns) / sr
        if gap > max(rules["truncation_gap_seconds"], rules["truncation_gap_fraction"] * duration):
            raise InvalidOutput("output_truncated", f"last turn ends {gap:.2f}s before the audio")
    turns.sort(key=lambda t: (t["start_sample"], t["end_sample"], t["speaker"]))
    by_speaker = {}
    for t in turns:
        by_speaker.setdefault(t["speaker"], []).append((t["start_sample"], t["end_sample"]))
    union = union_length([(t["start_sample"], t["end_sample"]) for t in turns])
    overlap_list = overlaps(turns)
    overlap = sum(o["end_sample"] - o["start_sample"] for o in overlap_list)
    dominant = max((union_length(v) for v in by_speaker.values()), default=0)
    return dict(
        audio_sha256=base["audio_sha256"],
        sample_rate=sr,
        num_frames=frames,
        speaker_count=len(by_speaker),
        turn_union_samples=union,
        overlap_samples=overlap,
        overlap_ratio=overlap / frames,
        dominant_speaker_ratio=dominant / union if union else None,
        turns=turns,
        overlaps=overlap_list,
        response_sha256=hashlib.sha256(raw).hexdigest(),
        response_bytes=len(raw),
    )
