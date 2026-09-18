"""One listener, one episode, the happy path -- against a faked outside world.

The server is the real thing, started as a real process and spoken to over HTTP.
Everything it would reach *out* to is one local stub that answers from canned data and
counts hits.  The test itself says what the outside world serves -- the feed, the audio,
the transcription API's reply, the model's reply -- and then what the listener gets.

The episode is built for the run, in :mod:`tests.integration.episode`: 73 minutes at the
timings of the real one this suite used to play, four breaks in it, silent.  The model's
reply is written here, quoting each break's own first and last words, and what those
quotes imply follows from `docs/spec.md`: a cut begins 1.5 s after the break's first word
and ends 1.5 s before its last, so a break loses its length less two margins and the
episode loses the four of them together.  The episode declares where the breaks are; the
margin comes from the spec; nothing here asks the server what it meant to do.

The only thing left real besides the server is ffmpeg, in Docker: "the file is shorter by
exactly what was removed" is a claim about audio, and a faked cutter would make it a claim
about nothing.
"""

from __future__ import annotations

import json

import pytest
import requests

from tests.integration.episode import MARGIN_SECONDS, quote
from tests.integration.support import decode_seconds, model_reply, podclean_server

def test_a_listener_subscribes_and_plays_one_episode(outside, tmp_path, episode, published):
    # What those quotes imply: every break but a margin at each end, and every chapter
    # earlier by all of the advertising that used to run before it.
    removed = sum(b.cut_seconds for b in episode.breaks)
    removed_before = {mark.title: sum(b.cut_seconds for b in episode.breaks
                                      if b.end <= mark.start)
                      for mark in episode.chapters}

    wanted = published
    # Room to send the episode in one request, which is what a transcriber that will take
    # the whole thing means: the play below asks it exactly once. The episode that is too
    # big to send whole is its own test, further down, and between them the two cover both
    # ways the audio can go up.
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:

        # Subscribe: the feed comes back with the audio link pointing here, nothing else touched.
        rss = requests.get(f"{podclean}/rss", params=wanted).text
        assert f"{podclean}/podcast?" in rss
        assert "<title>Oh Canada!</title>" in rss

        # Play: the publisher's audio minus the advertising.
        played = requests.get(f"{podclean}/podcast", params=wanted).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - removed, abs=1.0)

        # Play again: the same bytes, and nobody outside is asked anything a second time.
        hits_after_first_play = outside.counts()
        assert requests.get(f"{podclean}/podcast", params=wanted).content == played
        assert outside.counts() == hits_after_first_play
        assert hits_after_first_play["/audio/transcriptions"] == 1
        assert hits_after_first_play["/chat/completions"] == 1

        # A podcatcher asks whether the episode is there, and how big it is, before it
        # queues the download -- so this has to answer the way the download itself would.
        # Answering it 404 leaves the episode sitting at "waiting to download" forever,
        # which is what a real one did.
        head = requests.head(f"{podclean}/podcast", params=wanted)
        assert head.status_code == 200, "a podcatcher checking the episode was told it is not there"
        assert head.headers["Content-Length"] == str(len(played))
        assert head.content == b""

        # Pressing play, and resuming a download that dropped, are both byte ranges. A
        # server that answers one with the whole file makes a phone start an 86 MB episode
        # again from zero every time the connection goes, and tells a player that wants to
        # seek that it cannot.
        part = requests.get(f"{podclean}/podcast", params=wanted, headers={"Range": "bytes=0-99"})
        assert part.status_code == 206, "a range request was answered with the whole episode"
        assert part.headers["Content-Range"] == f"bytes 0-99/{len(played)}"
        assert part.headers["Accept-Ranges"] == "bytes"
        assert part.content == played[:100]

        # The audio is not text and has no encoding; saying it does is a claim about bytes
        # that are not characters.
        assert "charset" not in head.headers["Content-Type"]

        # The two documents describe the audio that was played: every mark and every line
        # moved up by exactly the audio removed before it, the advertising lines gone.
        chapters = requests.get(f"{podclean}/chapters", params=wanted).json()["chapters"]
        assert [c["title"] for c in chapters] == [m.title for m in episode.chapters]
        assert [c["startTime"] for c in chapters] == pytest.approx(
            [m.start - removed_before[m.title] for m in episode.chapters], abs=0.05)
        vtt = requests.get(f"{podclean}/transcript", params=wanted).text
        for brand in ("Northwind", "Cloudberry", "Tidewater", "Sandpiper"):
            assert brand not in vtt, f"the served transcript still reads out {brand}"
        assert "largest trading partner is Canada" in vtt


#: What the transcription endpoint will accept in one request, and what the server is told
#: to keep under.  The real endpoint's limit is 25 MB and this episode is about 35 at a
#: podcast's bitrate; these are small enough that it takes eight or nine pieces rather than
#: two, so a join is not a special case that happens once.
TRANSCRIBE_LIMIT = 5 << 20
CHUNK_UNDER = 4 << 20


def test_an_episode_too_big_for_one_transcription_request_is_cut_all_the_same(
        outside, tmp_path, episode, published):
    """The same episode, the same cut, when the transcriber will not take it in one go.

    The endpoint refuses a body over its limit, which is what the real one does -- an hour
    of a talk show at 192 kbps is three and a half times what it accepts.  So the server
    has to send the audio in pieces and put the answers back on one timeline.

    Everything asserted here is asserted in the same terms as the whole-episode play above:
    the length removed, and where every chapter lands.  The chapter marks are what make it
    a test of the arithmetic rather than of the plumbing -- they come from cues in the
    later pieces, so a piece put back at the wrong offset moves them, while a cut that
    merely happened would not.
    """
    outside.serves("/audio/transcriptions", "application/json", episode.transcriber(),
                   refuses_over=TRANSCRIBE_LIMIT)

    removed = sum(b.cut_seconds for b in episode.breaks)
    removed_before = {mark.title: sum(b.cut_seconds for b in episode.breaks
                                      if b.end <= mark.start)
                      for mark in episode.chapters}

    wanted = published
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(CHUNK_UNDER)) as podclean:
        requests.get(f"{podclean}/rss", params=wanted)
        played = requests.get(f"{podclean}/podcast", params=wanted).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - removed, abs=1.0)

        chapters = requests.get(f"{podclean}/chapters", params=wanted).json()["chapters"]
        assert [c["title"] for c in chapters] == [m.title for m in episode.chapters]
        assert [c["startTime"] for c in chapters] == pytest.approx(
            [m.start - removed_before[m.title] for m in episode.chapters], abs=0.05)

    # It really did take more than one request: otherwise the limit above was never
    # reached and this test says nothing the play above does not already say.
    assert outside.counts()["/audio/transcriptions"] > 1



def stop_segments_early(transcript, drop):
    """A transcription reply whose segments stop before its words do.

    A real one does this: the segments end while the words timed inside them keep going,
    and every one of those trailing words then belongs to the last segment that had
    started. Dropping the last ``drop`` segments and leaving every word alone is that
    artefact exactly -- nothing moves, the description of it just stops early.
    """
    reply = json.loads(transcript)
    reply["segments"] = reply["segments"][:-drop]
    return reply


def test_a_break_at_the_end_is_cut_when_the_transcriber_stops_before_its_words(
        outside, tmp_path, episode, published):
    """Advertising after the goodbye, described by a reply that stops short of it.

    The last cue of a real reply carries every word the transcriber timed after its
    segments ran out -- twenty seconds of them, in the episodes this was found on. A break
    anchored there is placed past that cue's own stated end, and a bound taken from that
    stated end refuses a cut that is perfectly correct.

    That is how the advertising at the end of two real episodes survived on 2026-09-18.
    The model reported both, at 0.95 and at 1.0 confidence, and both were thrown away.
    """
    reply = stop_segments_early(episode.transcript, 2)
    outside.serves("/audio/transcriptions", "application/json", reply)

    ending = [w for cue in episode.cues[-4:] for w in cue.words]
    opens, first, _ = quote(ending)
    closes, _, last = quote(ending, from_end=True)
    segment = {"start_cue": len(reply["segments"]) - 1, "end_cue": len(reply["segments"]),
               "category": "sponsor_read", "confidence": 0.95, "reason": "a post-roll",
               "first_words": opens, "last_words": closes}
    outside.serves("/chat/completions", "application/json", model_reply([segment]))
    removed = (last.end - MARGIN_SECONDS) - (first.start + MARGIN_SECONDS)

    wanted = published
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=wanted)
        played = requests.get(f"{podclean}/podcast", params=wanted).content
        assert played != episode.mp3.read_bytes(), (
            "the episode came back whole: the break at the end was refused because the "
            "transcriber's last segment ended before its own words did")
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - removed, abs=1.0)
