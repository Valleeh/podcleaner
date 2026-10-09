"""Untrusted plans serve the publisher's audio byte for byte.

These HTTP checks cover "Refusing" in docs/requirements.md without naming internal guards
or thresholds. An incomplete model reply also stays retryable and publishes no sidecars.
"""

from __future__ import annotations

import json

import requests

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
    # Not a schema this server can read, asked once more and mangled again: a stage that
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
            assert outside.counts()["/chat/completions"] == 2 * attempt


def test_a_reply_mangled_once_is_asked_for_again(outside, tmp_path, episode, published):
    # A provider once cut a reply off mid-JSON. Failing then serves every advertisement and
    # the podcatcher keeps that file for good, so the model is asked once more.
    answers = [{"choices": [{"message": {"content": '{"segments": [{"start_cue": 68, "end_'}}]},
               model_reply([b.as_segment() for b in episode.breaks])]
    outside.serves("/chat/completions", "application/json",
                   lambda _body: json.dumps(answers.pop(0)).encode("utf-8"))
    with podclean_server(outside, tmp_path) as server:
        requests.get(f"{server}/rss", params=published)
        played = requests.get(f"{server}/podcast", params=published).content
    assert len(played) < len(episode.mp3.read_bytes())
    assert outside.counts()["/chat/completions"] == 2


def test_a_break_whose_last_words_are_not_in_it_is_left_in(outside, tmp_path, episode, published):
    # The end of a cut is placed on the break's last words. Without them there is no way
    # to tell where the break ends from where the model stopped counting.
    read = episode.breaks[1]
    played = _play(outside, tmp_path, published, model_reply([
        {**read.as_segment(), "last_words": "words nobody said at the end"}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_whose_words_are_not_in_the_transcript_is_left_in(outside, tmp_path, episode, published):
    # Confident, well-formed, and quoting words nobody said. There is no way to place the
    # start from this, and a cut placed anyway is a cut placed by guessing.
    read = episode.breaks[1]
    played = _play(outside, tmp_path, published, model_reply([
        {**read.as_segment(reason="a read that is not in this episode"),
         "first_words": "brought to you by a sponsor nobody mentioned"}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_named_by_a_cue_the_transcript_never_had_is_left_in(outside, tmp_path, episode, published):
    # A timestamp where a cue number belongs -- the one mistake the prompt names, so it is
    # the one that has been seen. It matters because the cut runs to the end of the cue
    # named: clamp an index that was never rendered to the last cue there is and the cut
    # runs to the end of the episode, programme and all. A segment carrying one says
    # nothing this server can act on.
    read, later = episode.breaks[1], episode.breaks[2]
    played = _play(outside, tmp_path, published, model_reply([
        {**read.as_segment(reason="a read, with a timestamp where the cue number goes"),
         "end_cue": int(later.end)}]))
    assert played == episode.mp3.read_bytes()


def test_a_break_that_would_swallow_most_of_the_episode_is_not_cut(outside, tmp_path, episode, published):
    # The first advertising's own words and the last advertising's own last words, claimed
    # as one continuous break. Everything between them is the programme.
    first, last = episode.breaks[0], episode.breaks[-1]
    played = _play(outside, tmp_path, published, model_reply([
        {**first.as_segment(reason="claims the pre-roll, the programme and the promo block"),
         "end_cue": last.last_cue, "last_words": last.last_words}]))
    assert played == episode.mp3.read_bytes()
