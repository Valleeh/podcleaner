"""podcleaner.eval.wer: normalisation, WER arithmetic, anchors, phrase search."""

from __future__ import annotations

import pytest

from podcleaner.eval.wer import anchor_offsets, find_phrase, inner_wer, normalize_words, window_wer, word_error_rate
from podcleaner.transcripts import Cue, Transcript, Word


def test_normalize_words_symmetric_rules():
    assert normalize_words("<v Track 1>Herzlich willkommen, Ausgabe Nr. 490!") == ["herzlich", "willkommen", "ausgabe", "nr", "490"]
    assert normalize_words("[MUSIC] I'm Ina-Garten (laughs)") == ["i'm", "ina", "garten"]
    assert normalize_words("Wasserstoff-Gipfel 2025 ’quoted’") == ["wasserstoff", "gipfel", "2025", "quoted"]


def test_word_error_rate_hand_cases():
    r = word_error_rate("the cat sat on the mat", "the cat sat on the mat")
    assert r.wer == 0 and r.hits == 6
    r = word_error_rate("the cat sat on the mat", "the cat sat mat")
    assert r.deletions == 2 and r.wer == pytest.approx(2 / 6)
    r = word_error_rate("the cat", "the big cat")
    assert r.insertions == 1 and r.wer == pytest.approx(0.5)
    r = word_error_rate("the cat", "")
    assert r.wer == 1.0 and r.deletions == 2
    with pytest.raises(ValueError):
        word_error_rate("", "anything")


def test_window_wer_uses_cue_starts():
    ref = Transcript([Cue(1, 0, 2, "hello world"), Cue(2, 2, 4, "second cue"), Cue(3, 4, 6, "third")])
    hyp = Transcript([Cue(1, 0, 2, "hello world"), Cue(2, 2, 4, "second queue"), Cue(3, 4, 6, "third")])
    assert window_wer(ref, hyp, 0, 2).wer == 0
    assert window_wer(ref, hyp, 2, 4).wer == pytest.approx(0.5)


def test_anchor_offsets_with_and_without_word_timings():
    ref = Transcript([Cue(1, 10.0, 15.0, "one two three four five six seven")])
    words = tuple(Word(10.4 + 0.5 * k, 10.8 + 0.5 * k, w) for k, w in enumerate("one two three four five six seven".split()))
    hyp = Transcript([Cue(1, 10.3, 15.2, "one two three four five six seven", words=words)])
    [a] = anchor_offsets(ref, hyp)
    assert a.text == "one two three four" and a.delta == pytest.approx(0.4)
    hyp_nowords = Transcript([Cue(1, 10.3, 15.2, "one two three four five six seven")])
    [b] = anchor_offsets(ref, hyp_nowords)
    assert b.delta == pytest.approx(0.3)
    [c] = anchor_offsets(ref, Transcript([Cue(1, 10.0, 15.0, "completely different words entirely here now")]))
    assert c.hyp_time is None


def test_find_phrase_fuzzy_and_near():
    t = Transcript([
        Cue(1, 0, 3, "Welcome back to the show."),
        Cue(2, 974.5, 977.2, "Support for the show comes from Ground Noose."),
        Cue(3, 2000, 2003, "Support for the show comes from Ground News."),
    ])
    m = find_phrase(t, "Support for the show comes from Ground News.", near=974)
    assert m is not None and m.cue_index == 2 and m.ratio >= 0.85
    m2 = find_phrase(t, "Support for the show comes from Ground News.")
    assert m2 is not None and m2.cue_index == 3 and m2.ratio == 1.0
    assert find_phrase(t, "Bananas are yellow fruit indeed") is None


def test_inner_wer_ignores_window_edge_segmentation():
    words = [f"w{i}" for i in range(400)]
    reference = " ".join(words)
    # a window transcript that starts and ends mid-sentence relative to the reference
    hypothesis = " ".join(words[103:307])
    assert word_error_rate(" ".join(words[100:310]), hypothesis).wer > 0, "global alignment sees the edges"
    assert inner_wer(reference, hypothesis).wer == 0
    # a genuine recognition error in the middle still counts
    bad = words[103:307]
    bad[100] = "wrong"
    r = inner_wer(reference, " ".join(bad))
    assert r.substitutions == 1 and 0 < r.wer < 0.01
    with pytest.raises(ValueError):
        inner_wer(reference, "too short")
