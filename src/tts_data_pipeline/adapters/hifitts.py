"""HiFiTTS: join all embedded JSONL manifests to FLAC by complete relative path."""

import json
import re
import tarfile
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import inputs, lines, pairs, safe_member

MANIFEST = re.compile(r"(\d+)_manifest_(clean|other)_(train|dev|test)\.json$")


def input_files(root, config=None):
    return inputs(root, "*.tar.gz", config)


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        with pairs() as store, tarfile.open(path, "r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = safe_member(member.name)
                match = MANIFEST.fullmatch(PurePosixPath(name).name)
                updates = []
                if match:
                    speaker, quality, split = match.groups()

                    def metadata_updates():
                        for line in lines(archive, member):
                            meta = json.loads(line)
                            relative = safe_member(meta["audio_filepath"])
                            key = str(PurePosixPath(name).parent / relative)
                            yield (
                                key,
                                store.put(
                                    key,
                                    metadata={
                                        "upstream": meta,
                                        "speaker": speaker,
                                        "quality": quality,
                                        "split": split,
                                        "manifest": name,
                                    },
                                ),
                            )

                    updates = metadata_updates()
                elif name.endswith(".flac"):
                    updates.append((name, store.put(name, data=archive.extractfile(member).read())))
                for key, result in updates:
                    if result is None:
                        continue
                    data, meta = result
                    up = meta["upstream"]
                    text = up.get("text_no_preprocessing") or up.get("text") or None
                    parts = PurePosixPath(up["audio_filepath"]).parts
                    if parts[0] != "audio" or parts[1] != f"{meta['speaker']}_{meta['quality']}":
                        raise ValueError("HiFiTTS manifest speaker/path mismatch")
                    variants = [
                        {"kind": kind, "text": up[field], "language": "en"}
                        for field, kind in [
                            ("text", "source_preprocessed"),
                            ("text_normalized", "source_normalized"),
                        ]
                        if up.get(field)
                    ]
                    yield make_record(
                        dataset_id="hifitts",
                        source_snapshot=snapshot,
                        source_key=up["audio_filepath"],
                        audio_bytes=data,
                        audio_name=PurePosixPath(key).name,
                        source_locator={
                            "root": "raw_tts",
                            "path": f"HiFiTTS/{path.relative_to(root)}",
                            "member": key,
                            "metadata_member": meta["manifest"],
                        },
                        text=text,
                        text_kind="source_transcript" if text else None,
                        language="en",
                        speaker_id=f"hifitts:{meta['speaker']}",
                        speaker_scope="dataset",
                        source_config="all",
                        group_id=f"hifitts:book:{parts[2]}",
                        text_variants=variants,
                        metadata={
                            "original_split": meta["split"],
                            "quality_partition": meta["quality"],
                            "upstream": up,
                        },
                    )
            store.finish()
