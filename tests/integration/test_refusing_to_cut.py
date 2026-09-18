"""Untrusted plans serve the publisher's audio byte for byte.

These HTTP checks cover "Refusing" in docs/requirements.md without naming internal guards
or thresholds. An incomplete model reply also stays retryable and publishes no sidecars.
"""

from __future__ import annotations

import requests

import json

from tests.integration.episode import quote
from tests.integration.support import model_reply, podclean_server


def _play(outside, tmp_path, published, reply) -> bytes:
    """Play the episode with this classifier reply."""
    outside.serves("/chat/completions", "application/json", reply)

    with podclean_server(outside, tmp_path) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        played = requests.get(f"{podclean}/podcast", params=published)
        assert played.status_code == 200
        return played.content


def test_a_break_the_model_is_only_guessing_at_is_left_in(outside, tmp_path, episode, published):
    read = episode.breaks[1]
    played = _play(outside, tmp_path, published, model_reply([
        read.as_segment(confidence=0.01,
                        reason="it might be a read, it might be the hosts talking")]))
    assert played == episode.mp3.read_bytes()


def test_a_reply_the_model_mangles_leaves_the_episode_whole(outside, tmp_path, episode, published):
    # Not a schema this server can read, and not repairable by asking again: a stage that
    # did not finish is not a verdict about the audio.
    outside.serves("/chat/completions", "application/json", {
        "choices": [{"message": {"content": '{"segments": [{"start_cue": 68, "end_'}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with podclean_server(outside, tmp_path) as server:
        assert requests.get(f"{server}/rss", params=published).status_code == 200
        for attempt in range(1, 3):
            played = requests.get(f"{server}/podcast", params=published)
            assert played.status_code == 200
            assert played.content == episode.mp3.read_bytes()
            assert "charset" not in played.headers["Content-Type"]
            for route in ("chapters", "transcript"):
                assert requests.get(f"{server}/{route}", params=published).status_code == 404
            assert outside.counts()["/episode.mp3"] == attempt
            assert outside.counts()["/chat/completions"] == attempt


def test_a_break_whose_words_are_not_in_the_transcript_is_left_in(outside, tmp_path, episode, published):
    # Confident, well-formed, and quoting words nobody said. There is no way to place an
    # edge from this, and a cut placed anyway is a cut placed by guessing.
    read = episode.breaks[1]
    played = _play(outside, tmp_path, published, model_reply([
        {**read.as_segment(reason="a read that is not in this episode"),
         "first_words": "brought to you by a sponsor nobody mentioned",
         "last_words": "and that is the end of that"}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_named_by_a_cue_the_transcript_never_had_is_left_in(outside, tmp_path, episode, published):
    # A timestamp where a cue number belongs -- the one mistake the prompt names, so it is
    # the one that has been seen. It matters because the cue range is what bounds where the
    # quoted edges are looked for: widen it past the transcript and the quote places the cut
    # wherever those words next occur, which here is a later read with programme either
    # side of it. An index that was never rendered is not a small slip, and a segment
    # carrying one says nothing this server can act on.
    read, later = episode.breaks[1], episode.breaks[2]
    played = _play(outside, tmp_path, published, model_reply([
        {**read.as_segment(reason="a read, with a timestamp where the cue number goes"),
         "end_cue": int(later.end), "last_words": later.last_words}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_that_would_swallow_most_of_the_episode_is_not_cut(outside, tmp_path, episode, published):
    # Real quotes, from the first and the last advertising of the episode, claimed as one
    # continuous break. Everything between them is the programme.
    first, last = episode.breaks[0], episode.breaks[-1]
    played = _play(outside, tmp_path, published, model_reply([
        {**first.as_segment(reason="claims the pre-roll, the programme and the promo block"),
         "end_cue": last.last_cue, "last_words": last.last_words}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_whose_cue_is_stamped_late_in_the_middle_is_left_in(
        outside, tmp_path, episode, published):
    """The same artefact away from the end of the file means something else entirely.

    When a transcriber's segments stop short in the *middle*, its aligner has dropped a
    stretch of speech and parked the words that follow at the far end of the hole. The cue
    then covers up to half a minute of audio it says nothing about, and that audio is
    programme -- it is programme precisely because nothing transcribed it.

    Measured in the one real reply this project has kept: ninety-four cues of two hundred
    are stamped past their own end, four of them by more than ten seconds, the worst by
    twenty-eight. Letting a cut edge follow those timings would remove that speech with no
    trace of it in any document, because no cue ever mentioned it.

    So only the last cue of an episode may cover words stamped after it. Anywhere else the
    break is left in, which is the answer this project gives whenever an edge cannot be
    trusted.
    """
    reply = json.loads(episode.transcript)
    middle = len(reply["segments"]) // 2
    del reply["segments"][middle:middle + 2]
    outside.serves("/audio/transcriptions", "application/json", reply)

    named = [w for cue in episode.cues[middle - 2:middle + 2] for w in cue.words]
    outside.serves("/chat/completions", "application/json", model_reply([
        {"start_cue": middle - 1, "end_cue": middle, "category": "sponsor_read",
         "confidence": 0.95, "reason": "a break ending on a cue whose words are stamped late",
         "first_words": quote(named)[0], "last_words": quote(named, from_end=True)[0]}]))

    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as server:
        requests.get(f"{server}/rss", params=published)
        played = requests.get(f"{server}/podcast", params=published)
        assert played.status_code == 200
        assert played.content == episode.mp3.read_bytes(), (
            "a cut was placed on a word stamped after the cue that holds it, in the middle "
            "of the episode, where the audio in between is programme nothing transcribed")
