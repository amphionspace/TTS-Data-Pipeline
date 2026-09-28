"""StarRail Voice; preserve absent transcripts and character labels as null."""

from . import _game_voice

input_files = _game_voice.input_files


def iter_records(root, snapshot, *, files=None):
    yield from _game_voice.records(root, snapshot, files, "starrail_voice", "starrail-voice")
