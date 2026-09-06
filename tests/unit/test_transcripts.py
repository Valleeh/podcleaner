"""Offline tests for podcleaner.transcripts: parsers, windows, words."""

from __future__ import annotations

import json

import pytest

from podcleaner.transcripts import (
    Cue,
    Transcript,
    TranscriptError,
    is_non_speech,
    parse_srt,
    parse_vtt,
    parse_whisper_json,
)

SRT = """1
00:00:00,000 --> 00:00:01,560
[MUSIC]

2
00:00:01,560 --> 00:00:02,640
 I'm Ina Garten.

17
00:01:03.520 --> 00:01:06.800
Zwei Zeilen
hier
"""

VTT = """WEBVTT

NOTE this is a comment

00:00:00.005 --> 00:00:04.865
<v Track 1>Herzlich willkommen zur Lage der Nation, Ausgabe Nummer 490.

id-2
00:00:04.865 --> 00:00:08.614
<v Track 2>Und ich bin <b>Ulf</b>.

01:19:47.417 --> 01:19:47.734
Ciao.
"""


def test_parse_srt_reindexes_and_tolerates_both_separators():
    cues = parse_srt(SRT)
    assert [c.index for c in cues] == [1, 2, 3], "indices are ours, not the file's"
    assert cues[0].text == "[MUSIC]" and cues[0].start == 0.0 and cues[0].end == 1.56
    assert cues[1].text == "I'm Ina Garten."
    assert cues[2].start == pytest.approx(63.52) and cues[2].text == "Zwei Zeilen hier"


def test_parse_vtt_speakers_tags_and_headers():
    cues = parse_vtt(VTT)
    assert len(cues) == 3
    assert cues[0].speaker == "Track 1" and cues[0].text.startswith("Herzlich willkommen")
    assert cues[1].speaker == "Track 2" and cues[1].text == "Und ich bin Ulf."
    assert cues[2].speaker is None and cues[2].start == pytest.approx(4787.417)


def test_parse_vtt_rejects_non_vtt():
    with pytest.raises(TranscriptError):
        parse_vtt("1\n00:00:00,000 --> 00:00:01,000\nhi\n")


def _whisper_doc():
    return {
        "result": {"language": "en"},
        "params": {"model": "/models/ggml-small-q5_1.bin"},
        "model": {"type": "small"},
        "transcription": [
            {
                "offsets": {"from": 0, "to": 3000},
                "text": " Ask not, what",
                "tokens": [
                    {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}, "p": 0.9},
                    {"text": " Ask", "offsets": {"from": 320, "to": 400}, "p": 0.7},
                    {"text": " not", "offsets": {"from": 400, "to": 520}, "p": 0.9},
                    {"text": ",", "offsets": {"from": 520, "to": 530}, "p": 0.9},
                    {"text": " wh", "offsets": {"from": 700, "to": 800}, "p": 0.8},
                    {"text": "at", "offsets": {"from": 800, "to": 900}, "p": 0.8},
                    {"text": "[_TT_150]", "offsets": {"from": 3000, "to": 3000}, "p": 0.9},
                ],
            }
        ],
    }


def test_parse_whisper_json_groups_tokens_into_words_and_applies_offset():
    t = parse_whisper_json(_whisper_doc(), offset_seconds=100.0)
    assert t.language == "en"
    assert len(t.cues) == 1
    cue = t.cues[0]
    assert cue.start == 100.0 and cue.end == 103.0 and cue.text == "Ask not, what"
    words = [(w.text, round(w.start, 2), round(w.end, 2)) for w in cue.words]
    assert words == [("Ask", 100.32, 100.4), ("not,", 100.4, 100.53), ("what", 100.7, 100.9)]
    assert cue.words[0].p == 0.7


def test_parse_whisper_json_rejects_other_json():
    with pytest.raises(TranscriptError):
        parse_whisper_json({"cues": []})


def test_window_modes_and_reindex():
    t = Transcript([Cue(1, 0, 2, "a"), Cue(2, 2, 5, "b"), Cue(3, 5, 9, "c")])
    assert [c.text for c in t.window(2, 5).cues] == ["b"]
    assert [c.text for c in t.window(2, 5, mode="overlap").cues] == ["b"]
    assert [c.text for c in t.window(1, 6, mode="overlap").cues] == ["a", "b", "c"]
    assert [c.index for c in t.window(2, 9).cues] == [1, 2]
    with pytest.raises(TranscriptError):
        t.window(5, 2)


def test_shift_and_roundtrip(tmp_path):
    t = parse_whisper_json(_whisper_doc()).shifted(10.0)
    assert t.cues[0].start == 10.0 and t.cues[0].words[0].start == pytest.approx(10.32)
    path = tmp_path / "t.json"
    t.save(path)
    back = Transcript.load(path)
    assert back.to_dict() == t.to_dict()
    # generic JSON loader accepts whisper.cpp output too
    (tmp_path / "w.json").write_text(json.dumps(_whisper_doc()))
    from podcleaner.transcripts import load_transcript

    assert load_transcript(tmp_path / "w.json").cues[0].text == "Ask not, what"


@pytest.mark.parametrize(
    "text,expected",
    [("[MUSIC]", True), ("(laughs)", True), ("  ", True), ("[MUSIC] and words", False), ("Hello", False)],
)
def test_is_non_speech(text, expected):
    assert is_non_speech(text) is expected
