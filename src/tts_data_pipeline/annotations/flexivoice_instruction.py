"""Style-only instruction revision for selected FlexiVoice subsets (immutable annotation).

FlexiVoice instructions for EARS and Expresso are ParaSpeechCaps captions that mix the speaker's
voice (gender, accent, timbre) and habitual traits (typical pitch, loudness, pace) with the delivery,
and often contradict the style the speaker was asked to perform. That recording-protocol style is in
the upstream path (EARS `p042/sentences_02_whisper.wav`, Expresso `.../read/ex01/happy/...`). An LLM
writes one instruction per (protocol style, caption): it must express the protocol style, may keep
compatible delivery nuance from the caption, and must not describe the speaker or the recording.

Three phases, each resumable:

    collect   distinct instructions of the target subsets -> <work>/instructions.jsonl
    generate  one LLM call per distinct instruction        -> <work>/generations.jsonl
              (an OpenAI-compatible server, e.g. sglang serving the pinned model)
    publish   one row per base sample                      -> annotations/flexivoice_instruction/<run>

Rows outside the target subsets are `skipped/not_requested`; target rows whose rewrite keeps no
style are `skipped/no_style`; failed or unparsable generations are `failed/generation_error`; an
instruction that still names the speaker or the recording is `failed/speaker_description`.
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.parquet as pq

from ..contract import annotation_sample_schema
from ..feature_runtime import file_hash, write_json
from ..schema import digest
from ..timestamps import BEIJING
from .upstream_quality.publication import publish_output
from .upstream_quality.run import pin_source

TASK = "flexivoice_instruction"
# Recording-protocol style labels from the upstream paths, shown to the LLM in words.
LABELS = {
    "regular": "neutral, normal reading (no particular emotion or manner)",
    "highpitch": "higher pitch than the speaker's normal voice",
    "lowpitch": "lower pitch than the speaker's normal voice",
    "fast": "fast speaking rate",
    "slow": "slow speaking rate",
    "loud": "loud",
    "whisper": "whispering",
    "projected": "projected, loud voice as if speaking to someone at a distance",
    "enunciated": "clearly enunciated, over-articulated",
    "laughing": "laughing while speaking",
    "singing": "singing",
}
SYSTEM_PROMPT = """You write style instructions for a text-to-speech system. The speaker's voice (timbre) always comes from a reference recording; the instruction describes ONLY how this utterance is delivered.

You get two inputs:
1. RECORDING STYLE: the style the speaker was asked to perform when this utterance was recorded. It is ground truth.
2. DESCRIPTION: an automatic caption of the recording. It often describes the speaker's permanent voice and habits (gender, accent, timbre, typical pitch, typical loudness, typical pace, habitual sing-song intonation) and may contradict the recording style.

Write one natural, concise instruction addressed to the speaker that:
- MUST express the recording style.
- MAY add delivery details from the description only if they are emotion nuance, attitude, articulation, hesitation or pauses, and only if compatible with the recording style.
- MUST NOT mention speaking rate, pace, loudness, volume, pitch level or sing-song/melodic intonation unless the recording style itself is about them.
- MUST NOT mention gender, age, accent, nationality, timbre or voice texture (husky, nasal, raspy, deep, shrill, silky, booming voice...), recording environment or audio quality.
- For a neutral recording style, ask for a natural, neutral delivery.
Write in English. Vary your wording naturally. Reply with JSON only: {"style": "<instruction>"}"""
# A generated instruction that still names the speaker or the recording is rejected
# (failed/speaker_description) rather than published.
SPEAKER_TERMS = (
    r"\b(male|female|man|woman|boy|girl|accent|american|british|nasal|husky|raspy|shrill|"
    r"guttural|silky|environment|recording|microphone|noisy|background noise)\b"
)
GENERATION = {"temperature": 0.7, "max_tokens": 200, "enable_thinking": False}


def recording_style(config, wav_path):
    """Recording-protocol style from the upstream path (EARS / Expresso)."""
    parts = wav_path.split("/")
    name = parts[-1].rsplit(".", 1)[0]
    if config == "ears":
        return f"emotion: {name.split('_')[1]}" if name.startswith("emo_") else name.split("_")[-1]
    if config == "expresso":
        return parts[-3] if "/read/" in wav_path else parts[-2]
    raise ValueError(f"No recording-protocol style for {config}")


def unit(config, wav_path, instruction):
    """Generation unit: (recording style, source instruction)."""
    style = recording_style(config, wav_path)
    return style, sha(json.dumps([style, instruction], ensure_ascii=False))


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def model_identity(model_path):
    model_path = Path(model_path)
    files = ["config.json", "model.safetensors.index.json", "tokenizer.json", "chat_template.jinja"]
    return {
        "path": str(model_path),
        "files": {f: file_hash(model_path / f) for f in files if (model_path / f).exists()},
    }


def profile(configs, model):
    return {
        "task": TASK,
        "name": "flexivoice-style-instruction-v1",
        "recording_style_labels": LABELS,
        "execution_kind": "llm_text_rewrite",
        "target_source_configs": sorted(configs),
        "model": model,
        "system_prompt_sha256": sha(SYSTEM_PROMPT),
        "system_prompt": SYSTEM_PROMPT,
        "generation": GENERATION,
        "rejected_terms": SPEAKER_TERMS,
        "result": "instruction revision {action: replace, instruction}: the recording-protocol "
        "style (from the upstream path) expressed as an instruction, with compatible delivery "
        "details of the source caption; one generation per distinct (style, source instruction)",
    }


def result_type():
    return pa.struct(
        [
            pa.field("action", pa.string()),
            pa.field("instruction", pa.string()),
            pa.field("recording_style", pa.string()),
            pa.field("source_instruction_sha256", pa.string()),
            pa.field("removed", pa.string()),
        ]
    )


def target_rows(root, source, configs):
    base = lance.dataset(root / source["table_path"], version=source["lance_version"])
    quoted = ", ".join(f"'{c}'" for c in sorted(configs))
    for batch in base.scanner(
        columns=["sample_id", "source_config", "metadata_json"],
        filter=f"source_config IN ({quoted})",
        batch_size=65536,
    ).to_batches():
        for sid, config, meta in zip(*(batch.column(c).to_pylist() for c in batch.column_names)):
            meta = json.loads(meta)
            yield config, meta["upstream"]["wav_path"], (meta.get("instruction") or "").strip()


def collect(root, work, configs):
    root, work = Path(root).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = pin_source(root, "flexivoice")[0]
    counts = Counter()
    for config, wav_path, text in target_rows(root, source, configs):
        counts[(recording_style(config, wav_path), text)] += 1
    with (work / "instructions.jsonl").open("w") as out:
        for (style, text), n in sorted(counts.items()):
            key = sha(json.dumps([style, text], ensure_ascii=False))
            out.write(json.dumps({"sha256": key, "recording_style": style, "instruction": text,
                                  "rows": n}, ensure_ascii=False) + "\n")
    write_json(work / "collect.json", {"source": source, "configs": sorted(configs),
                                       "rows": sum(counts.values()), "distinct": len(counts)})
    print(json.dumps({"rows": sum(counts.values()), "distinct": len(counts)}), flush=True)


def call(url, served, style, text, seed):
    shown = LABELS.get(style, style)
    body = {
        "model": served,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user",
                      "content": f"RECORDING STYLE: {shown}\nDESCRIPTION: {text}"}],
        "seed": seed,
        "temperature": GENERATION["temperature"],
        "max_tokens": GENERATION["max_tokens"],
        "chat_template_kwargs": {"enable_thinking": GENERATION["enable_thinking"]},
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)["choices"][0]["message"]["content"]


def parse(raw):
    try:
        value = json.loads(raw)
        style, removed = value["style"], value.get("removed") or ""
        if not isinstance(style, str):
            raise TypeError
        return style.strip(), str(removed)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, None


def generate(work, url, served, workers):
    work = Path(work)
    done = set()
    out_path = work / "generations.jsonl"
    if out_path.exists():
        done = {json.loads(line)["sha256"] for line in out_path.open()}
    todo = [json.loads(line) for line in (work / "instructions.jsonl").open()]
    todo = [t for t in todo if t["sha256"] not in done]
    print(json.dumps({"remaining": len(todo), "done": len(done)}), flush=True)

    def one(item):
        try:
            raw = call(url, served, item["recording_style"], item["instruction"],
                       int(item["sha256"][:8], 16))
        except Exception as exc:  # recorded and retried by a later generate pass
            return {"sha256": item["sha256"], "error": str(exc)[:300]}
        style, removed = parse(raw)
        return {"sha256": item["sha256"], "raw": raw, "style": style, "removed": removed}

    with cf.ThreadPoolExecutor(workers) as pool, out_path.open("a") as out:
        for i, result in enumerate(pool.map(one, todo), 1):
            if "error" in result:
                continue
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            if i % 1000 == 0:
                print(json.dumps({"generated": i, "of": len(todo)}), flush=True)


def publish(root, work, model_path):
    root, work = Path(root).resolve(), Path(work).resolve()
    collected = json.loads((work / "collect.json").read_text())
    configs = set(collected["configs"])
    source = pin_source(root, "flexivoice")[0]
    if source != collected["source"]:
        raise ValueError("Base changed since collect")
    generations = {}
    for line in (work / "generations.jsonl").open():
        item = json.loads(line)
        generations[item["sha256"]] = item
    distinct = [json.loads(line)["sha256"] for line in (work / "instructions.jsonl").open()]
    missing = [s for s in distinct if s not in generations]
    if missing:
        raise ValueError(f"{len(missing)} instructions have no generation; rerun generate")
    prof = profile(configs, model_identity(model_path))
    stamp = datetime.now(BEIJING)
    run_id = f"tts-ann-{TASK}-{stamp:%Y%m%dT%H%M%S}bjt-01"
    plan = {
        "source": source,
        "profile": prof,
        "run_id": run_id,
        "implementation_sha256": file_hash(__file__),
        "instructions_sha256": file_hash(work / "instructions.jsonl"),
        "generations_sha256": file_hash(work / "generations.jsonl"),
    }
    write_json(work / "plan.json", plan)
    profile_id = digest(prof)
    schema = annotation_sample_schema(result_type())
    base = lance.dataset(root / source["table_path"], version=source["lance_version"])
    counts, errors, actions = Counter(), Counter(), Counter()
    scratch = work / "rows.parquet"
    with pq.ParquetWriter(scratch, schema) as writer:
        for batch in base.scanner(
            columns=["sample_id", "audio_sha256", "source_config", "metadata_json"],
            scan_in_order=True,
            batch_size=8192,
        ).to_batches():
            rows = []
            for r in batch.to_pylist():
                meta = json.loads(r["metadata_json"])
                text = (meta.get("instruction") or "").strip()
                key, style = sha(text), None
                if r["source_config"] in configs:
                    style, key = unit(r["source_config"], meta["upstream"]["wav_path"], text)
                fingerprint = digest([TASK, r["sample_id"], r["audio_sha256"], key, profile_id])
                row = {"sample_id": r["sample_id"], "input_fingerprint": fingerprint,
                       "status": "skipped", "error_code": None, "result": None}
                if r["source_config"] not in configs:
                    row["error_code"] = "not_requested"
                else:
                    gen = generations[key]
                    if gen["style"] is None:
                        row.update(status="failed", error_code="generation_error")
                    elif not gen["style"]:
                        row["error_code"] = "no_style"
                    elif re.search(SPEAKER_TERMS, gen["style"], re.IGNORECASE):
                        row.update(status="failed", error_code="speaker_description")
                    else:
                        row.update(status="ok", result={
                            "action": "replace", "instruction": gen["style"],
                            "recording_style": style, "source_instruction_sha256": sha(text),
                            "removed": gen["removed"]})
                counts[row["status"]] += 1
                if row["error_code"]:
                    errors[row["error_code"]] += 1
                if row["result"]:
                    actions[row["result"]["action"]] += 1
                rows.append(row)
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
    stats = {"error_codes": dict(errors), "actions": dict(actions),
             "distinct_instructions": len(distinct)}
    output = publish_output(
        root, source, run_id, work,
        lambda: pq.ParquetFile(scratch).iter_batches(batch_size=8192),
        counts, stats, task=TASK, schema=schema,
    )
    manifest = {
        "artifact_kind": "annotation",
        "contract_version": "v0.1",
        "schema_version": "flexivoice-instruction-v1",
        "status": "complete",
        "task": TASK,
        "target_kind": "sample",
        "run_id": run_id,
        "profile": prof,
        "profile_id": profile_id,
        "inputs": [source],
        "outputs": [output],
        "execution": plan,
        "finished_at": datetime.now(BEIJING).isoformat(),
    }
    destination = root / "annotations" / TASK / run_id / "manifest.json"
    write_json(destination, manifest)
    print(json.dumps({"manifest": str(destination), "counts": counts, "statistics": stats}),
          flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="phase", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--root", type=Path, required=True)
    c.add_argument("--work", type=Path, required=True)
    c.add_argument("--configs", nargs="+", default=["ears", "expresso"])
    g = sub.add_parser("generate")
    g.add_argument("--work", type=Path, required=True)
    g.add_argument("--url", default="http://127.0.0.1:30871/v1/chat/completions")
    g.add_argument("--served-model", default="qwen27b")
    g.add_argument("--workers", type=int, default=64)
    p = sub.add_parser("publish")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--model-path", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "collect":
        collect(args.root, args.work, set(args.configs))
    elif args.phase == "generate":
        generate(args.work, args.url, args.served_model, args.workers)
    else:
        publish(args.root, args.work, args.model_path)


if __name__ == "__main__":
    main()
