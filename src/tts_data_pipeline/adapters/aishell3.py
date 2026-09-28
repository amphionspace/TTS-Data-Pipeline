"""AISHELL-3: alternate word/pinyin tokens; preserve words, pinyin and speaker scope."""

import tarfile
from pathlib import PurePosixPath

from ..schema import make_record
from ._archives import inputs, lines, pairs, safe_member


def input_files(root, config=None):
    return inputs(root, "*.tgz", config)


def parse_content(line):
    filename, content = line.rstrip("\r\n").split("\t", 1)
    tokens = content.split()
    if not tokens or len(tokens) % 2:
        raise ValueError(f"Invalid AISHELL alternating word/pinyin: {filename}")
    return filename, "".join(tokens[::2]), " ".join(tokens[1::2]), content


def iter_records(root, snapshot, *, files=None):
    for path in files if files is not None else input_files(root):
        speakers = {}
        with pairs() as store, tarfile.open(path, "r|*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                name = safe_member(member.name)
                parts = PurePosixPath(name).parts
                updates = []
                if PurePosixPath(name).name == "spk-info.txt":
                    for line in lines(archive, member):
                        if line.strip() and not line.startswith("#"):
                            key, *fields = line.split()
                            speakers[key] = fields
                elif PurePosixPath(name).name == "content.txt":
                    split = parts[-2]

                    def metadata_updates():
                        for line in lines(archive, member):
                            filename, text, pinyin, original = parse_content(line)
                            key = str(PurePosixPath(name).parent / "wav" / filename[:7] / filename)
                            yield (
                                key,
                                store.put(
                                    key,
                                    metadata={
                                        "text": text,
                                        "pinyin": pinyin,
                                        "original": original,
                                        "split": split,
                                        "metadata_member": name,
                                    },
                                ),
                            )

                    updates = metadata_updates()
                elif name.endswith(".wav"):
                    if len(parts) < 4 or parts[-3] != "wav":
                        raise ValueError(f"Unexpected AISHELL WAV path: {name}")
                    split, speaker = parts[-4], parts[-2]
                    key = name
                    if speaker != parts[-1][:7]:
                        raise ValueError("AISHELL speaker/path mismatch")
                    updates.append((key, store.put(key, data=archive.extractfile(member).read())))
                for key, result in updates:
                    if result is None:
                        continue
                    data, meta = result
                    filename = PurePosixPath(key).name
                    speaker = filename[:7]
                    audio_member = (
                        f"{PurePosixPath(meta['metadata_member']).parent}/wav/{speaker}/{filename}"
                    )
                    yield make_record(
                        dataset_id="aishell3",
                        source_snapshot=snapshot,
                        source_key=f"{meta['split']}/{filename}",
                        audio_bytes=data,
                        audio_name=filename,
                        source_locator={
                            "root": "raw_tts",
                            "path": f"AISHELL-3/{path.relative_to(root)}",
                            "member": audio_member,
                            "metadata_member": meta["metadata_member"],
                        },
                        text=meta["text"],
                        text_kind="source_transcript",
                        language="zh",
                        speaker_id=f"aishell3:{speaker}",
                        speaker_scope="dataset",
                        source_config="all",
                        text_variants=[
                            {"kind": "source_pinyin", "text": meta["pinyin"], "language": "zh-Latn"}
                        ],
                        metadata={
                            "original_split": meta["split"],
                            "upstream_word_pinyin": meta["original"],
                            "upstream_speaker_info": speakers.get(speaker),
                        },
                    )
            store.finish()
