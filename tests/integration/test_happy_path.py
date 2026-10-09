"""One listener, one episode, the happy path -- against a faked outside world.

The server is the real thing, started as a real process and spoken to over HTTP.
Everything it would reach *out* to is one local stub that answers from canned data and
counts hits.  The test itself says what the outside world serves -- the feed, the audio,
the transcription API's reply, the model's reply -- and then what the listener gets.

The episode is built for the run, in :mod:`tests.integration.episode`: 73 minutes at the
timings of the real one this suite used to play, four breaks in it, silent.  The model's
reply is written here, naming each break's cues and quoting its first words, and what
that implies follows from `README.md`: a cut begins 1.5 s after the break's first word
and ends 1.5 s before its last cue does, so a break loses that span less two margins and
the episode loses the four of them together.  The episode declares where the breaks are;
the margin comes from the README; nothing here asks the server what it meant to do.

The only thing left real besides the server is ffmpeg, in Docker: "the file is shorter by
exactly what was removed" is a claim about audio, and a faked cutter would make it a claim
about nothing.
"""

from __future__ import annotations

import json

import pytest
import requests

from tests.integration.episode import MARGIN_SECONDS, quote, removed
from tests.integration.support import decode_seconds, ffmpeg, model_reply, podclean_server

def test_a_listener_subscribes_and_plays_one_episode(outside, tmp_path, episode, published):
    # What the reply implies: every break but a margin at each end, and every chapter
    # earlier by all of the advertising that used to run before it.
    lost = removed(episode.breaks)
    lost_before = {mark.title: removed(episode.breaks, before=mark.start)
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
            decode_seconds(episode.mp3) - lost, abs=1.0)

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
            [m.start - lost_before[m.title] for m in episode.chapters], abs=0.05)
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

    lost = removed(episode.breaks)
    lost_before = {mark.title: removed(episode.breaks, before=mark.start)
                   for mark in episode.chapters}

    wanted = published
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(CHUNK_UNDER)) as podclean:
        requests.get(f"{podclean}/rss", params=wanted)
        played = requests.get(f"{podclean}/podcast", params=wanted).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - lost, abs=1.0)

        chapters = requests.get(f"{podclean}/chapters", params=wanted).json()["chapters"]
        assert [c["title"] for c in chapters] == [m.title for m in episode.chapters]
        assert [c["startTime"] for c in chapters] == pytest.approx(
            [m.start - lost_before[m.title] for m in episode.chapters], abs=0.05)

    # It really did take more than one request: otherwise the limit above was never
    # reached and this test says nothing the play above does not already say.
    assert outside.counts()["/audio/transcriptions"] > 1


def test_an_episode_hours_long_is_cut_all_the_same(outside, tmp_path, episode, published):
    """Some podcasts run four or five hours, and the listener chose to subscribe to them.

    The episode played three times over, back to back: nearly four hours, as long as the
    one this was found on. Its breaks are all in the first third, where the transcript and
    the model put them, so it loses exactly what the episode on its own loses.
    """
    (tmp_path / "episode.mp3").write_bytes(episode.mp3.read_bytes())
    (tmp_path / "parts.txt").write_text("file '/in/episode.mp3'\n" * 3)
    long = tmp_path / "long.mp3"
    ffmpeg(["-f", "concat", "-safe", "0", "-i", "/in/parts.txt", "-c", "copy", "-y"],
           reads=tmp_path / "parts.txt", writes=long)
    outside.serves("/episode.mp3", "audio/mpeg", long.read_bytes())
    outside.serves("/audio/transcriptions", "application/json", episode.transcriber())

    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        played = requests.get(f"{podclean}/podcast", params=published).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(long) - removed(episode.breaks), abs=1.0)


@pytest.mark.parametrize("rounds", [1, 2], ids=["filled-at-once", "filled-in-two"])
def test_a_stretch_the_transcriber_skipped_is_asked_for_again(
        outside, tmp_path, episode, published, rounds):
    """A real transcriber now and then answers a stretch of speech with nothing at all.

    It happened at the jingle of nearly every break in one episode, and for ten minutes
    after a foreign-language spot in it. Words nobody transcribed cannot be quoted, so a
    break inside such a hole is cut late or not at all. Sent the hole again on its own,
    the transcriber answered about it -- though after that spot only about the spot, and
    the rest came back when what was still missing was asked about once more. So the
    server asks until nothing is missing, and the episode loses every break, the skipped
    one included, exactly as if nothing had been skipped.
    """
    outside.serves("/audio/transcriptions", "application/json",
                   episode.transcriber_skipping(episode.breaks[1], rounds=rounds))

    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        played = requests.get(f"{podclean}/podcast", params=published).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - removed(episode.breaks), abs=1.0)
    assert outside.counts()["/audio/transcriptions"] == 1 + rounds


def test_a_quote_said_twice_in_its_break_is_cut_from_the_later_one(
        outside, tmp_path, episode, published):
    """A spot played twice back to back, named as one break, opens with the same words twice.

    The last one in the episode this was found on did: forty seconds of it, refused whole
    because its quote could be found in two places. Either place is inside what the model
    named; the later one removes less, so that is where the cut starts -- the second
    playing goes, and the first stays in rather than risk a second of anything else.
    """
    brk = episode.breaks[0]
    later = next(c for c in episode.cues if c.number == brk.first_cue + 2)
    assert later.number < brk.last_cue
    reply = json.loads(episode.transcript)
    said = {round(w.start, 3): text
            for w, text in zip(later.words, brk.first_words.split())}
    for word in reply["words"]:
        if word["start"] in said:
            word["word"] = f" {said[word['start']]}"
    outside.serves("/audio/transcriptions", "application/json", reply)
    outside.serves("/chat/completions", "application/json", model_reply([brk.as_segment()]))
    lost = (brk.end - MARGIN_SECONDS) - (later.start + MARGIN_SECONDS)

    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        played = requests.get(f"{podclean}/podcast", params=published).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - lost, abs=1.0)


def test_the_transcriber_is_told_the_language_the_feed_declares(
        outside, tmp_path, episode, published):
    """Left to guess, it guessed from the first thing it heard.

    An English episode that opened on a German advertisement came back with its first
    minutes translated into German, so nothing the hosts said there could be quoted. The
    feed says what language it is in; the transcriber is told.
    """
    outside.serves("/feed.xml", "application/rss+xml", f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Solved</title><language>en-us</language>
<item><title>Failure</title><guid>{published['guid']}</guid>
<enclosure url="{outside.url}/episode.mp3" type="audio/mpeg"/></item></channel></rss>""")
    sent = []

    def transcribe(body):
        sent.append(body)
        return episode.transcript

    outside.serves("/audio/transcriptions", "application/json", transcribe)
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        assert requests.get(f"{podclean}/podcast", params=published).status_code == 200
    assert sent and all(b'name="language"\r\n\r\nen\r\n' in body for body in sent)



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
    segments ran out -- twenty seconds of them, in the episodes this was found on. A cut
    ends where its last cue ends, so a last cue that ended where the reply said it did
    would stop the cut short of the break's own words and leave most of a post-roll in.
    The last cue, and only the last cue, ends where its words end.
    """
    reply = stop_segments_early(episode.transcript, 2)
    outside.serves("/audio/transcriptions", "application/json", reply)

    ending = [w for cue in episode.cues[-4:] for w in cue.words]
    opens, first = quote(ending)
    segment = {"start_cue": len(reply["segments"]) - 1, "end_cue": len(reply["segments"]),
               "category": "sponsor_read", "confidence": 0.95, "reason": "a post-roll",
               "first_words": opens}
    outside.serves("/chat/completions", "application/json", model_reply([segment]))
    lost = (episode.cues[-1].end - MARGIN_SECONDS) - (first.start + MARGIN_SECONDS)

    wanted = published
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=wanted)
        played = requests.get(f"{podclean}/podcast", params=wanted).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - lost, abs=1.0), (
            "the break at the end was not cut to its words: the transcriber's last "
            "segment ended before they did")


def test_a_cut_in_the_middle_ends_where_its_cue_does_and_not_where_its_words_were_parked(
        outside, tmp_path, episode, published):
    """The same artefact away from the end of the file means something else entirely.

    When a transcriber's segments stop short in the *middle*, its aligner has dropped a
    stretch of speech and parked the words that follow at the far end of the hole. The cue
    then has words stamped up to half a minute past its own end, and the audio in between
    is programme -- it is programme precisely because nothing transcribed it.

    So a cut ends where the cue's own description of itself ends, never where its parked
    words do: everything a cue said nothing about stays in.
    """
    reply = json.loads(episode.transcript)
    middle = len(reply["segments"]) // 2
    del reply["segments"][middle:middle + 2]
    outside.serves("/audio/transcriptions", "application/json", reply)

    named = [w for cue in episode.cues[middle - 2:middle + 2] for w in cue.words]
    opens, first = quote(named)
    outside.serves("/chat/completions", "application/json", model_reply([
        {"start_cue": middle - 1, "end_cue": middle, "category": "sponsor_read",
         "confidence": 0.95, "reason": "a break ending on a cue whose words are stamped late",
         "first_words": opens}]))
    lost = (episode.cues[middle - 1].end - MARGIN_SECONDS) - (first.start + MARGIN_SECONDS)

    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as podclean:
        requests.get(f"{podclean}/rss", params=published)
        played = requests.get(f"{podclean}/podcast", params=published).content
        (tmp_path / "played.mp3").write_bytes(played)
        assert decode_seconds(tmp_path / "played.mp3") == pytest.approx(
            decode_seconds(episode.mp3) - lost, abs=1.0), (
            "the cut reached past the cue's own end, into audio no cue described")
