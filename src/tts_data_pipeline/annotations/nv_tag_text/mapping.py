"""nv-canonical-v1: rewrite each paralinguistic source's text into NVSpeech-style tags.

Similar sounds map onto the 18 NVSpeech (Emilia-NV) tags; distinct sounds without an NVSpeech
tag get one of 8 extended tags in the same `[Name]` form. A tag stays where the source put its
event. Every function returns (text, record) where record lists the source tokens, target tags
and any repairs, or raises on input this version does not know.
"""

import re

VERSION = "nv-canonical-v1"
NVSPEECH_TAGS = (
    "Breathing Laughter Cough Sigh Confirmation-en Question-en Question-ah Question-oh "
    "Surprise-ah Surprise-oh Dissatisfaction-hnn Uhm Shh Crying Surprise-wa Surprise-yo "
    "Question-ei Question-yi"
).split()
EXTENDED_TAGS = "Sniff Throat-clearing Gasp Yawn Groan Snore Sneeze Mouth-noise".split()
TAGS = frozenset(NVSPEECH_TAGS) | frozenset(EXTENDED_TAGS)

SMIIP = {"laughter": "Laughter", "cough": "Cough", "crying": "Crying"}
NVS = {
    "sigh": "Sigh",
    "sniff": "Sniff",
    "laughing": "Laughter",
    "coughing": "Cough",
    "throatclearing": "Throat-clearing",
    "breath": "Breathing",
    "crying": "Crying",
    "gasp": "Gasp",
    "yawn": "Yawn",
    "snore": "Snore",
}
# NonverbalTTS emoji; the meanings are not published. Ranked by frequency they follow the
# paper's Table 2 order exactly (Breath > Laugh > Sniff > Cough > Throat > Sigh > Groan >
# Sneeze > Snore > Grunt), and each emoji fits that sound.
NVTTS = {
    "🌬": "Breathing",
    "🤣": "Laughter",
    "👃": "Sniff",
    "😷": "Cough",
    "🗣": "Throat-clearing",
    "😤": "Sigh",
    "😖": "Groan",
    "🤧": "Sneeze",
    "😴": "Snore",
    "🐖": "Dissatisfaction-hnn",
}
DISFLUENCY = {
    "laughter": "Laughter",
    "breathing": "Breathing",
    "inhaling": "Breathing",
    "sigh": "Sigh",
    "cough": "Cough",
    "throat_clearing": "Throat-clearing",
    "throat_clearings": "Throat-clearing",
    "sniffing": "Sniff",
    "snorting": "Sniff",
    "gasp": "Gasp",
    "groan": "Groan",
    "lipsmack": "Mouth-noise",
    "lip_smack": "Mouth-noise",
    "lipsmacking": "Mouth-noise",
    "swallowing": "Mouth-noise",
}
SOURCE_TAGS = dict(
    smiip_nv={f"<{k}>": v for k, v in SMIIP.items()},
    nonverbalspeech38k={f"[{k}]": v for k, v in NVS.items()},
    nonverbaltts=NVTTS,
    disfluencyspeech={f"<{k}>": v for k, v in DISFLUENCY.items()},
    emilia_nv={f"[{k}]": k for k in NVSPEECH_TAGS},
)
TAG = re.compile(r"\[([^\[\]]+)\]")
SPACES = re.compile(r"[ \t]+")


def record(source, target, fixes=()):
    return dict(
        source_tags=source,
        target_tags=target,
        extended=any(t not in NVSPEECH_TAGS for t in target),
        fixes=list(fixes),
    )


def tidy(text):
    """Collapse runs of spaces and trim; nothing else in the text changes."""
    return SPACES.sub(" ", text).strip()


def check(text, label):
    rest = TAG.sub("", text)
    if any(c in rest for c in "[]<>{}") or any(t not in TAGS for t in TAG.findall(text)):
        raise ValueError(f"{label}: unconverted markup remains: {text!r}")
    return text


def emilia_nv(text):
    """Already NVSpeech; checked and kept."""
    found = TAG.findall(text)
    if any(t not in NVSPEECH_TAGS for t in found):
        raise ValueError(f"emilia_nv: unknown tag in {text!r}")
    return text, record([f"[{t}]" for t in found], found)


SMIIP_TAG = re.compile(r"<(laughter|cough|crying)>")
# The six malformed clips seen in SMIIP-NV v0.1 main text, repaired to the tag they name.
SMIIP_FIXES = (
    ("doubled_open_bracket", re.compile(r"<<(laughter|cough|crying)>"), r"<\1>"),
    ("doubled_close_bracket", re.compile(r"<(laughter|cough|crying)>>"), r"<\1>"),
    ("missing_open_bracket", re.compile(r"(?<![</a-z])(laughter|cough|crying)>"), r"<\1>"),
    ("misspelled_laughter", re.compile(r"<aughter>"), "<laughter>"),
    ("stray_open_bracket", re.compile(r"<(?![a-z/]+>)"), ""),
)


def smiip_nv(text):
    fixes = []
    for name, pattern, replacement in SMIIP_FIXES:
        text, count = pattern.subn(replacement, text)
        fixes += [name] * count
    source = SMIIP_TAG.findall(text)
    target = [SMIIP[s] for s in source]
    text = SMIIP_TAG.sub(lambda m: f"[{SMIIP[m.group(1)]}]", text)
    return check(tidy(text), "smiip_nv"), record([f"<{s}>" for s in source], target, fixes)


NVS_TAG = re.compile(r"\[([a-z]+)\]")
NVS_REGION = re.compile(r"</?B>")


def nonverbalspeech38k(text):
    text, regions = NVS_REGION.subn("", text)
    source = NVS_TAG.findall(text)
    if any(s not in NVS for s in source):
        raise ValueError(f"nonverbalspeech38k: unknown tag in {text!r}")
    text = NVS_TAG.sub(lambda m: f"[{NVS[m.group(1)]}]", text)
    fixes = ["removed_region_markup"] * (regions // 2) if regions else []
    if regions % 2:
        raise ValueError(f"nonverbalspeech38k: unbalanced <B> markup: {text!r}")
    return (
        check(tidy(text), "nonverbalspeech38k"),
        record([f"[{s}]" for s in source], [NVS[s] for s in source], fixes),
    )


EMOJI = re.compile("[" + "".join(NVTTS) + "]️?")
OTHER_SYMBOL = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")


def nonverbaltts(text):
    source = [m.group(0)[0] for m in EMOJI.finditer(text)]
    text = EMOJI.sub(lambda m: f"[{NVTTS[m.group(0)[0]]}]", text)
    # Upstream fusion sometimes dropped an emoji but left its U+FE0F variation selector.
    text, orphans = re.subn("\ufe0f", "", text)
    if OTHER_SYMBOL.search(text):
        raise ValueError(f"nonverbaltts: unknown symbol in {text!r}")
    return check(tidy(text), "nonverbaltts"), record(
        source, [NVTTS[s] for s in source], ["orphan_variation_selector"] * orphans
    )


DISFLUENCY_TAG = re.compile(r"<([A-Za-z_]+)>")
BRACE = re.compile(r"\{[A-Z] ([^{}]*)\}")
UNCERTAIN = re.compile(r"\(\(\s*(.*?)\s*\)\)")
INTERRUPTION = re.compile(r"(?<!\S)-{1,2}(?!\S)")
SPACE_BEFORE_PUNCTUATION = re.compile(r" +([,.?!])")
REPEATED_COMMA = re.compile(r",(?: *,)+")


def disfluencyspeech(text):
    """Switchboard markup back to the spoken words; uh/um stay words (filled pauses)."""
    source = DISFLUENCY_TAG.findall(text)
    if any(s.lower() not in DISFLUENCY for s in source):
        raise ValueError(f"disfluencyspeech: unknown event in {text!r}")
    target = [DISFLUENCY[s.lower()] for s in source]
    text = DISFLUENCY_TAG.sub(lambda m: f"[{DISFLUENCY[m.group(1).lower()]}]", text)
    while BRACE.search(text):  # nested asides: innermost first
        text = BRACE.sub(r"\1", text)
    # A few upstream lines open or close an aside without its partner; drop the stray marker.
    text, opened = re.subn(r"\{[A-Z] ", "", text)
    text, closed = re.subn(r"\}", "", text)
    fixes = ["unbalanced_brace"] * (opened + closed)
    text = UNCERTAIN.sub(r"\1", text)
    text = re.sub(r"(?<!\S)[\[\]+](?!\S)", " ", text)  # repair brackets: keep both attempts
    text = text.replace("[ ", " ").replace(" ]", " ")
    text = INTERRUPTION.sub(" ", text)
    text = SPACE_BEFORE_PUNCTUATION.sub(r"\1", SPACES.sub(" ", text))
    text = REPEATED_COMMA.sub(",", text).strip(" ,")
    return check(tidy(text), "disfluencyspeech"), record([f"<{s}>" for s in source], target, fixes)


CONVERTERS = dict(
    emilia_nv=emilia_nv,
    smiip_nv=smiip_nv,
    nonverbalspeech38k=nonverbalspeech38k,
    nonverbaltts=nonverbaltts,
    disfluencyspeech=disfluencyspeech,
)


def definition():
    """The full mapping, as written into the annotation profile."""
    return dict(
        version=VERSION,
        nvspeech_tags=NVSPEECH_TAGS,
        extended_tags=EXTENDED_TAGS,
        source_tags=SOURCE_TAGS,
        tag_format="[Name]",
        placement="in place of the source event marker",
        repairs=dict(
            smiip_nv=[name for name, _, _ in SMIIP_FIXES],
            nonverbalspeech38k=["removed_region_markup (<B>...</B>, text kept)"],
            nonverbaltts=["orphan_variation_selector (U+FE0F left without its emoji)"],
            disfluencyspeech=[
                "{C|D|E|F|A ...} unwrapped, words kept (filled pauses uh/um stay words)",
                "[ reparandum + repair ] keeps both attempts",
                "(( ... )) uncertain transcription unwrapped",
                "standalone - and -- interruption marks removed",
                "unbalanced_brace (stray {X or } dropped)",
            ],
        ),
        notes=dict(
            nonverbaltts="emoji meanings inferred: frequency order equals the paper's Table 2",
        ),
    )
