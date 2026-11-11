"""Speaker identity and text comparison for the FlexiVoice selection policy."""

import re
import unicodedata


def speaker_key(config, wav_path):
    """Speaker key within FlexiVoice, or None where the path names no speaker (Emilia,
    nvspeech, MSP-PODCAST, MELD, m3ed)."""
    p = wav_path.split("/")
    fn = p[-1]
    if config in ("genshin", "starrail"):
        return f"{config}:{p[3]}:{p[4]}"  # language / character: one voice actor per language
    if config in ("ears", "l2"):
        return f"{config}:{p[3]}"
    if config == "expresso":
        if "_channel" in fn:  # conversational: <a>-<b>/…_channel1_… is speaker a
            a, b = p[5].split("-")
            return f"expresso:{a if '_channel1_' in fn else b}"
        return f"expresso:{p[5]}"
    if config == "kespeech":
        return f"kespeech:{p[4]}"
    if config == "voxceleb":
        return f"voxceleb:{p[6]}"
    if config == "childmandarin":
        return f"childmandarin:{fn.split('_')[0]}"
    if config == "WHSP":
        return f"WHSP:{p[4]}"
    if config == "debatts":
        return "debatts:" + "_".join(fn.split("_")[:2])
    if config == "emotion":
        src = p[2]
        if src == "Emotion_Speech_Dataset":
            return f"ESD:{p[3]}"
        if src == "CREMA-D":
            return f"CREMA-D:{fn.split('_')[0]}"
        if src == "RAVDESS":
            return f"RAVDESS:{p[3]}"
        if src == "EMNS":
            return "EMNS:single"
        if src == "IEMOCAP":  # Ses03M_impro05b_M029: session 3, utterance by the male speaker
            return f"IEMOCAP:{p[3]}-{fn.rsplit('_', 1)[1][0]}"
    return None


def utterance_key(text):
    text = unicodedata.normalize("NFKC", re.sub(r"\[[A-Za-z]+(?:-[A-Za-z]+)*\]", "", text)).lower()
    return "".join(c for c in text if c.isalnum())
