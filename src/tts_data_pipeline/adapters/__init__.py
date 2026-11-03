"""One module per source dataset; registry contains implemented adapters only."""

from . import (
    aishell3,
    csemotions,
    disfluencyspeech,
    emilia,
    emilia_nv,
    emilia_yodas,
    galgame,
    genshin_voice,
    hifitts,
    hifitts2,
    libriheavy,
    libritts_r,
    ljspeech,
    mls_sidon,
    nonverbalspeech38k,
    nonverbaltts,
    smiip_nv,
    starrail_voice,
    vctk,
    wenetspeech4tts,
    wutheringwaves,
    zenless_voice,
)

ADAPTERS = {
    "emilia": emilia,
    "emilia_nv": emilia_nv,
    "emilia_yodas": emilia_yodas,
    "aishell3": aishell3,
    "ljspeech": ljspeech,
    "vctk": vctk,
    "hifitts": hifitts,
    "hifitts2": hifitts2,
    "wenetspeech4tts": wenetspeech4tts,
    "wutheringwaves": wutheringwaves,
    "genshin_voice": genshin_voice,
    "starrail_voice": starrail_voice,
    "galgame": galgame,
    "csemotions": csemotions,
    "libritts_r": libritts_r,
    "libriheavy": libriheavy,
    "mls_sidon": mls_sidon,
    "zenless_voice": zenless_voice,
    "nonverbaltts": nonverbaltts,
    "disfluencyspeech": disfluencyspeech,
    "nonverbalspeech38k": nonverbalspeech38k,
    "smiip_nv": smiip_nv,
}


def identity_scheme(dataset):
    return getattr(ADAPTERS[dataset], "IDENTITY_SCHEME", "source-file-v1")
