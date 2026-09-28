"""Genshin Voice; character labels are scoped by dubbing language."""

from . import _game_voice

input_files = _game_voice.input_files


def iter_records(root, snapshot, *, files=None):
    yield from _game_voice.records(root, snapshot, files, "genshin_voice", "genshin-voice")
