"""Verify metadata joining, source semantics and dependency-aware publication."""

import io
import json
import tarfile
import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import soundfile as sf

from tts_data_pipeline.adapters import ADAPTERS, wenetspeech4tts
from tts_data_pipeline.bulk import bulk_convert, check_inputs, source_plan
from tts_data_pipeline.convert import convert
from tts_data_pipeline.writer import release_dataset


def audio():
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600), 16000, format="FLAC")
    return buf.getvalue()


def archive(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:bz2" if path.name.endswith(".bz2") else "w:gz"
    with tarfile.open(path, mode) as tar:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))


def fixture(root, dataset, reverse=False):
    if dataset == "ljspeech":
        path = root / "LJSpeech.tar.bz2"
        members = [
            ("LJ/metadata.csv", b"LJ001-1|Original text.|Normalized text."),
            ("LJ/wavs/LJ001-1.wav", audio()),
        ]
    elif dataset == "aishell3":
        path = root / "aishell.tgz"
        members = [
            ("test/content.txt", "SSB00010001.wav\t你 ni3 好 hao3\n".encode()),
            ("test/wav/SSB0001/SSB00010001.wav", audio()),
        ]
    elif dataset == "hifitts":
        path = root / "hifi.tar.gz"
        meta = {
            "audio_filepath": "audio/1_clean/book/clip.flac",
            "text": "Processed",
            "text_no_preprocessing": "Original",
            "text_normalized": "Normalized",
        }
        members = [
            ("hifi/1_manifest_clean_dev.json", json.dumps(meta).encode()),
            ("hifi/audio/1_clean/book/clip.flac", audio()),
        ]
    else:
        tier = "Premium"
        prefix = f"WenetSpeech4TTS_{tier}_0"
        key = "X1_S00001"
        path = root / tier / (prefix + ".tar.gz")
        members = [
            (f"{prefix}/txts/{key}.txt", f"{key}\t你好\n\t0.0 0.1\n".encode()),
            (f"{prefix}/wavs/{key}.wav", audio()),
        ]
        for sub in ["filelists", "DNSMOS_P808Scores"]:
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "filelists/Basic_filelist.lst").write_text(
            f"{key}\t../{tier}/{prefix}/wavs/{key}.wav\t../{tier}/{prefix}/txts/{key}.txt\n"
        )
        (root / "DNSMOS_P808Scores/Basic_DNSMOS.lst").write_text(f"{key}\t4.2\n")
    archive(path, list(reversed(members)) if reverse else members)
    return path, members


@pytest.mark.parametrize("dataset", ["aishell3", "ljspeech", "hifitts", "wenetspeech4tts"])
def test_archive_join_independent_of_audio_metadata_order(tmp_path, dataset):
    root = tmp_path / "raw"
    path, _ = fixture(root, dataset)
    before = list(ADAPTERS[dataset].iter_records(root, "fixed"))
    fixture(root, dataset, reverse=True)
    after = list(ADAPTERS[dataset].iter_records(root, "fixed"))
    assert before == after and len(after) == 1
    r = after[0]
    meta = json.loads(r["metadata_json"])
    assert r["source_split"] == "train" and r["audio"]["bytes"] == audio()
    if dataset == "aishell3":
        assert r["text"] == "你好" and r["text_variants"][0]["text"] == "ni3 hao3"
        assert meta["original_split"] == "test"
    elif dataset == "ljspeech":
        assert r["text_variants"][0]["text"] == "Normalized text."
    elif dataset == "hifitts":
        assert r["text"] == "Original" and meta["original_split"] == "dev"
    else:
        assert r["speaker_id"] is None and meta["upstream_dnsmos_p808"] == 4.2
        assert meta["logical_subsets"] == ["Basic", "Standard", "Premium"]
    result = convert(dataset, root, tmp_path / "release", deep_verify=True)
    assert result["rows"] == 1 and release_dataset(tmp_path / "release").count_rows() == 1


@pytest.mark.parametrize("dataset", ["aishell3", "ljspeech", "hifitts", "wenetspeech4tts"])
@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_path"])
def test_archive_pairing_fails_closed(tmp_path, dataset, failure):
    path, members = fixture(tmp_path, dataset)
    if failure == "missing":
        members = members[:1]
    elif failure == "duplicate":
        members.append(members[-1])
    else:
        members[-1] = ("wrong/" + members[-1][0], members[-1][1])
    archive(path, members)
    with pytest.raises(ValueError):
        list(ADAPTERS[dataset].iter_records(tmp_path, "fixed"))


def test_vctk_microphones_and_missing_transcript(tmp_path):
    with zipfile.ZipFile(tmp_path / "vctk.zip", "w") as z:
        z.writestr("txt/p225/p225_001.txt", "Hello")
        for speaker, mic in [("p225", "mic1"), ("p225", "mic2"), ("p315", "mic1")]:
            z.writestr(f"wav48_silence_trimmed/{speaker}/{speaker}_001_{mic}.flac", audio())
    rows = list(ADAPTERS["vctk"].iter_records(tmp_path, "fixed"))
    assert len({r["sample_id"] for r in rows}) == 3
    assert rows[0]["group_id"] == rows[1]["group_id"]
    assert rows[2]["text"] is None


def test_wenet_dependency_changes_identity_and_resume_checks_hash(tmp_path):
    root = tmp_path / "raw"
    fixture(root, "wenetspeech4tts")
    batch = source_plan("wenetspeech4tts", root, 1024**2)[0]
    a = tmp_path / "a"
    b = tmp_path / "b"
    bulk_convert("wenetspeech4tts", root, a, workers=1, deep_verify=True)
    score = root / "DNSMOS_P808Scores/Basic_DNSMOS.lst"
    score.write_text("X1_S00001\t4.3\n")
    with pytest.raises(ValueError, match="changed"):
        check_inputs(root, batch)
    convert("wenetspeech4tts", root, b, deep_verify=True)
    before = release_dataset(a).to_table().to_pylist()[0]
    after = release_dataset(b).to_table().to_pylist()[0]
    assert before["sample_id"] != after["sample_id"]
    assert before["audio_sha256"] == after["audio_sha256"]
    assert json.loads((b / "manifest.json").read_text())["identity_scheme"] == "source-unit-v1"
    assert wenetspeech4tts.input_files(root, "Basic") == wenetspeech4tts.input_files(
        root, "Premium"
    )


@pytest.mark.parametrize("dataset", ["genshin_voice", "starrail_voice", "galgame"])
def test_game_namespace_nulls_and_original_ids(tmp_path, dataset):
    folder = tmp_path / ("game-A" if dataset == "galgame" else "data")
    folder.mkdir()
    rows = [
        {
            "audio": {"bytes": audio(), "path": "clip.flac"},
            "transcription": "Hi",
            "speaker": "Role",
            "language": lang,
            "text": "こんにちは",
            "audio_ID": "original-ID",
        }
        for lang in ["Japanese", "English(US)"]
    ]
    rows.append({**rows[0], "speaker": "", "transcription": ""})
    pq.write_table(pa.Table.from_pylist(rows), folder / "train.parquet")
    result = list(ADAPTERS[dataset].iter_records(tmp_path, "fixed"))
    assert len({r["sample_id"] for r in result}) == 3
    if dataset == "galgame":
        assert all(r["speaker_id"] is None and r["source_config"] == "game-A" for r in result)
        assert json.loads(result[0]["metadata_json"])["upstream_audio_id"] == "original-ID"
    else:
        assert result[0]["speaker_id"] != result[1]["speaker_id"]
        assert result[2]["text"] is None and result[2]["speaker_id"] is None
    assert source_plan(dataset, tmp_path, 1024**2)


@pytest.mark.parametrize(
    "source,expected", [("Chinese", "zh"), ("English", "en"), ("", None), (None, None)]
)
def test_game_all_observed_language_forms(tmp_path, source, expected):
    folder = tmp_path / "data"
    folder.mkdir()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "audio": {"bytes": audio(), "path": "a.flac"},
                    "language": source,
                    "speaker": "Role",
                    "transcription": "hello",
                }
            ]
        ),
        folder / "train.parquet",
    )
    row = next(ADAPTERS["genshin_voice"].iter_records(tmp_path, "test"))
    assert row["language"] == expected
    assert (row["speaker_id"] is None) == (expected is None)
    assert json.loads(row["metadata_json"])["upstream"]["language"] == source


@pytest.mark.parametrize(
    "config,directory,language",
    [
        ("CN", "中文 - Chinese", "zh"),
        ("EN", "英语 - English", "en"),
        ("JP", "日语 - Japanese", "ja"),
        ("KR", "韩语 - Korean", "ko"),
    ],
)
def test_wuthering_language_role_and_preview_match_full(tmp_path, config, directory, language):
    import py7zr

    from tts_data_pipeline.preview import preview

    root = tmp_path / "raw"
    root.mkdir()
    with py7zr.SevenZipFile(root / f"WutheringWaves2.2_{config}.7z", "w") as z:
        for role in ["角色", "None", "？？？？"]:
            prefix = f"{directory}/{role}/带变量语音 - Placeholder/clip"
            z.writestr("Hello {name}\n".encode(), prefix + ".lab")
            z.writestr(audio(), prefix + ".wav")
    module = ADAPTERS["wutheringwaves"]
    rows = list(module.iter_records(root, "test"))
    assert len(rows) == 3 and {r["language"] for r in rows} == {language}
    assert rows[0]["speaker_id"].startswith(f"wutheringwaves:{language}:")
    assert rows[1]["speaker_id"] is None and rows[2]["speaker_id"] is None
    assert json.loads(rows[0]["metadata_json"])["category_path"] == ["带变量语音 - Placeholder"]
    assert rows[:1] == list(
        module.iter_preview_records(root, "test", files=module.input_files(root), limit=1)
    )
    assert preview("wutheringwaves", root, "test", tmp_path / "preview", 1)["audio_bytes_preserved"]
    result = convert("wutheringwaves", root, tmp_path / "release", deep_verify=True)
    assert result["rows"] == 3


def test_wuthering_rejects_missing_text_before_extraction(tmp_path):
    import py7zr

    with py7zr.SevenZipFile(tmp_path / "WutheringWaves2.2_EN.7z", "w") as z:
        z.writestr(audio(), "英语 - English/Role/clip.wav")
    with pytest.raises(ValueError, match="Unpaired"):
        list(ADAPTERS["wutheringwaves"].iter_records(tmp_path, "test"))
