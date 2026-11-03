"""nv-canonical-v1 mapping rules and the nv_tag_text text-revision publication."""

import json

import lance
import pytest
from test_nv_parquet import disfluency

from tts_data_pipeline.annotations.nv_tag_text import mapping
from tts_data_pipeline.annotations.nv_tag_text import run as nv
from tts_data_pipeline.bulk import bulk_convert


def test_tag_set_is_the_26_trainer_tokens():
    assert len(mapping.NVSPEECH_TAGS) == 18 and len(mapping.EXTENDED_TAGS) == 8
    assert mapping.EXTENDED_TAGS == [
        "Sniff",
        "Throat-clearing",
        "Gasp",
        "Yawn",
        "Groan",
        "Snore",
        "Sneeze",
        "Mouth-noise",
    ]
    targets = {v for table in mapping.SOURCE_TAGS.values() for v in table.values()}
    assert targets <= mapping.TAGS


@pytest.mark.parametrize(
    "dataset, source, expected, tags, fixes",
    [
        ("smiip_nv", "孤单<crying>。", "孤单[Crying]。", ["Crying"], []),
        ("smiip_nv", "对象<<crying>。", "对象[Crying]。", ["Crying"], ["doubled_open_bracket"]),
        (
            "smiip_nv",
            "照片<laughter>>求",
            "照片[Laughter]求",
            ["Laughter"],
            ["doubled_close_bracket"],
        ),
        ("smiip_nv", "沉痛crying>仿佛", "沉痛[Crying]仿佛", ["Crying"], ["missing_open_bracket"]),
        (
            "smiip_nv",
            "目标儿<aughter>，",
            "目标儿[Laughter]，",
            ["Laughter"],
            ["misspelled_laughter"],
        ),
        (
            "smiip_nv",
            "嗯<laughter><但只写了",
            "嗯[Laughter]但只写了",
            ["Laughter"],
            ["stray_open_bracket"],
        ),
        ("nonverbalspeech38k", "[throatclearing]<B>是</B>你", "[Throat-clearing]是你", None, None),
        ("nonverbalspeech38k", "[sniff]母后", "[Sniff]母后", ["Sniff"], []),
        (
            "nonverbaltts",
            "So, Mom, 🌬️ how 🐖 are 👃 you? ️",
            "So, Mom, [Breathing] how [Dissatisfaction-hnn] are [Sniff] you?",
            ["Breathing", "Dissatisfaction-hnn", "Sniff"],
            ["orphan_variation_selector"],
        ),
        (
            "disfluencyspeech",
            "{F Oh, } {D well, } [ They're, + {F uh, } they're ] fine <Lipsmack>. -",
            "Oh, well, They're, uh, they're fine [Mouth-noise].",
            ["Mouth-noise"],
            [],
        ),
        (
            "disfluencyspeech",
            "[ is it, + {A {F oh } goodness } it's, ] (( he )) <throat_clearing> -- ok",
            "is it, oh goodness it's, he [Throat-clearing] ok",
            ["Throat-clearing"],
            [],
        ),
        ("disfluencyspeech", "{D you know, that's", "you know, that's", [], ["unbalanced_brace"]),
        (
            "emilia_nv",
            "[Uhm]嗯。[Question-ei]",
            "[Uhm]嗯。[Question-ei]",
            ["Uhm", "Question-ei"],
            [],
        ),
    ],
)
def test_conversion_rules(dataset, source, expected, tags, fixes):
    text, info = mapping.CONVERTERS[dataset](source)
    assert text == expected
    if tags is not None:
        assert info["target_tags"] == tags and info["fixes"] == fixes
        assert info["extended"] == any(t in mapping.EXTENDED_TAGS for t in tags)


@pytest.mark.parametrize(
    "dataset, source",
    [
        ("nonverbalspeech38k", "[hiccup]你好"),
        ("nonverbaltts", "Hi 🙂 there"),
        ("disfluencyspeech", "ok <hiccup>"),
        ("emilia_nv", "[Sniff]嗯"),
        ("smiip_nv", "好<sneeze>的"),
    ],
)
def test_unknown_markup_is_rejected(dataset, source):
    with pytest.raises(ValueError):
        mapping.CONVERTERS[dataset](source)


def test_publishes_text_revision_rows(tmp_path):
    root = tmp_path / "root"
    raw = tmp_path / "raw"
    disfluency(raw)
    bulk_convert(
        "disfluencyspeech",
        raw,
        root / "datasets/disfluencyspeech/v0.1",
        workers=1,
        deep_verify=True,
    )
    path = nv.run(root, tmp_path / "work", ("disfluencyspeech",))
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "complete" and manifest["profile"]["name"] == "nv-canonical-v1"
    output = manifest["outputs"][0]
    assert output["coverage"]["ok"] == 3 and output["statistics"]["target_tags"] == {"Laughter": 3}
    rows = lance.dataset(root / output["table"]["table_path"]).to_table().to_pylist()
    first = rows[0]["result"]
    assert first["action"] == "replace" and first["text"] == "Yeah. [Laughter] Well, rabbits."
    assert first["source_tags"] == ["<laughter>"] and first["extended"] is False
    assert nv.run(root, tmp_path / "work", ("disfluencyspeech",)) == path
