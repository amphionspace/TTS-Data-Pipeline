"""Zenless Voice; preserve game captions and language-scoped character labels."""

from . import _game_voice

input_files = _game_voice.input_files


def iter_records(root, snapshot, *, files=None):
    yield from _game_voice.records(root, snapshot, files, "zenless_voice", "zenless-voice")
