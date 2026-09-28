"""LJSpeech 1.1: retain original and normalized transcripts, one known speaker."""

import tarfile
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import inputs, lines, pairs, safe_member


def input_files(root, config=None):
    return inputs(root, "*.tar.bz2", config)


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        with pairs() as store, tarfile.open(path, "r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = safe_member(member.name)
                updates = []
                if PurePosixPath(name).name == "metadata.csv":

                    def metadata_updates():
                        for line in lines(archive, member):
                            key, text, normalized = line.rstrip("\r\n").split("|", 2)
                            yield (
                                str(PurePosixPath(name).parent / "wavs" / (key + ".wav")),
                                store.put(
                                    str(PurePosixPath(name).parent / "wavs" / (key + ".wav")),
                                    metadata={
                                        "text": text,
                                        "normalized": normalized,
                                        "metadata_member": name,
                                    },
                                ),
                            )

                    updates = metadata_updates()
                elif name.endswith(".wav"):
                    key = name
                    updates.append((key, store.put(key, data=archive.extractfile(member).read())))
                for key, result in updates:
                    if result is None:
                        continue
                    data, meta = result
                    key = PurePosixPath(key).stem
                    audio_member = f"{PurePosixPath(meta['metadata_member']).parent}/wavs/{key}.wav"
                    yield make_record(
                        dataset_id="ljspeech",
                        source_snapshot=snapshot,
                        source_key=key,
                        audio_bytes=data,
                        audio_name=key + ".wav",
                        source_locator={
                            "root": "raw_tts",
                            "path": f"LJSpeech/{path.relative_to(root)}",
                            "member": audio_member,
                            "metadata_member": meta["metadata_member"],
                        },
                        text=meta["text"] or None,
                        text_kind="source_transcript" if meta["text"] else None,
                        language="en",
                        speaker_id="ljspeech:LJ",
                        speaker_scope="dataset",
                        source_config="all",
                        group_id=f"ljspeech:{key.split('-')[0]}",
                        text_variants=[
                            {
                                "kind": "source_normalized",
                                "text": meta["normalized"],
                                "language": "en",
                            }
                        ]
                        if meta["normalized"]
                        else [],
                        metadata={"original_split": None, "upstream_id": key},
                    )
            store.finish()
