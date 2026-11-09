"""FlexiVoice instruction records; repeated audio references remain separate samples."""

import json
from pathlib import Path, PurePosixPath

from ..schema import make_record

IDENTITY_SCHEME = "source-unit-v1"
CONFIGS = (
    "WHSP",
    "childmandarin",
    "debatts",
    "ears",
    "emilia_en_ds",
    "emilia_psc",
    "emilia_zh_ds",
    "emotion",
    "expresso",
    "genshin",
    "kespeech",
    "l2",
    "nvspeech",
    "starrail",
    "voxceleb",
)
LANGUAGES = {"EN": "en", "ZH": "zh", "JA": "ja", "KO": "ko"}


def input_files(root, config=None):
    configs = CONFIGS if config in (None, "all") else (config,)
    if any(c not in CONFIGS for c in configs):
        raise ValueError(f"Unknown FlexiVoice config: {config}")
    paths = [root / "resolved_json" / f"{c}.json" for c in configs]
    if any(not p.is_file() for p in paths):
        raise ValueError("Missing FlexiVoice resolved JSON")
    if config in (None, "all") and set((root / "resolved_json").glob("*.json")) != set(paths):
        raise ValueError("Unexpected FlexiVoice resolved JSON")
    return sorted(paths)


def records(path):
    with path.open() as handle:
        data = json.load(handle)
    if not isinstance(data, list) or not data:
        raise ValueError(f"Expected nonempty JSON array: {path}")
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Expected object at {path}:{index}")
        for key in ("text", "instruction"):
            if key not in row or not isinstance(row[key], (str, type(None))):
                raise ValueError(f"Invalid {key} at {path}:{index}")
        yield index, row


def audio_path(root, row):
    value = row.get("wav_path")
    if not isinstance(value, str):
        raise ValueError("Missing wav_path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "audio"
        or str(relative) != value
    ):
        raise ValueError(f"Unsafe audio path: {value}")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping audio: {value}")
    return path


def dependency_files(root, path):
    # Pin every external audio object, including its SHA256, in source-unit-v1.
    return sorted({audio_path(root, row) for _, row in records(path)})


def language_of(value):
    parts = PurePosixPath(value).parts
    if len(parts) > 2 and parts[1] == "emilia":
        return LANGUAGES.get(parts[2])
    if len(parts) > 3 and parts[1] == "instruction_data" and parts[2] in {"genshin", "starrail"}:
        return LANGUAGES.get(parts[3])
    # Instructions can be in a different language from the spoken audio.
    return None


def iter_records(root: Path, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        config = path.stem
        if config not in CONFIGS:
            raise ValueError(f"Unknown FlexiVoice subset: {config}")
        for index, upstream in records(path):
            audio = audio_path(root, upstream)
            text = upstream["text"]
            text = text if text and text.strip() else None
            yield make_record(
                dataset_id="flexivoice",
                source_snapshot=snapshot,
                source_key=f"{config}:{index}",
                source_config=config,
                source_locator={
                    "root": "flexivoice",
                    "path": str(path.relative_to(root)),
                    "row": index,
                    "audio_path": upstream["wav_path"],
                },
                audio_bytes=audio.read_bytes(),
                audio_name=audio.name,
                text=text,
                text_kind="source_transcript" if text else None,
                language=language_of(upstream["wav_path"]),
                group_id=f"flexivoice:audio:{upstream['wav_path']}",
                metadata={"instruction": upstream["instruction"], "upstream": upstream},
            )
