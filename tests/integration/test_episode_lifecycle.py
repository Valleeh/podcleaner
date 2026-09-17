"""First plays share work; completed decisions keep documents and survive a restart."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import requests

from tests.integration.support import model_reply, podclean_server


def test_simultaneous_first_plays_only_pay_once(outside, tmp_path, episode, published):
    started, release = Event(), Event()

    def transcribe(_body):
        started.set()
        assert release.wait(10), "the test never released transcription"
        return episode.transcript

    outside.serves("/audio/transcriptions", "application/json", transcribe)
    with podclean_server(outside, tmp_path,
                         PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as server:
        assert requests.get(f"{server}/rss", params=published).status_code == 200
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(requests.get, f"{server}/podcast", params=published, timeout=30)
            try:
                assert started.wait(10), "the first play never reached transcription"
                second = pool.submit(requests.get, f"{server}/podcast", params=published, timeout=30)
                # Keep the first play in flight while the second reaches the server.
                assert not second.done()
                Event().wait(0.5)
            finally:
                release.set()
            answers = [first.result(), second.result()]

    assert [answer.status_code for answer in answers] == [200, 200]
    assert answers[0].content == answers[1].content
    assert outside.counts()["/episode.mp3"] == 1
    assert outside.counts()["/audio/transcriptions"] == 1
    assert outside.counts()["/chat/completions"] == 1


@pytest.mark.parametrize("refuse", [False, True], ids=["clean", "refused"])
def test_untouched_audio_keeps_its_documents_and_is_not_reexamined(
        outside, tmp_path, episode, published, refuse):
    segments = [{**episode.breaks[0].as_segment(), "first_words": "words nobody ever said"}] if refuse else []
    mark = episode.chapters[0]
    outside.serves("/chat/completions", "application/json", model_reply(
        segments, [{"cue": mark.cue, "title": mark.title}]))

    with podclean_server(outside, tmp_path) as server:
        assert requests.get(f"{server}/rss", params=published).status_code == 200
        audio = requests.get(f"{server}/podcast", params=published)
        assert audio.status_code == 200
        assert audio.content == episode.mp3.read_bytes()
        chapters = requests.get(f"{server}/chapters", params=published)
        transcript = requests.get(f"{server}/transcript", params=published)
        assert chapters.status_code == transcript.status_code == 200
        assert chapters.json()["chapters"] == [
            {"startTime": pytest.approx(mark.start, abs=0.001), "title": mark.title}]
        assert "Northwind" in transcript.text
        hits = outside.counts()

    with podclean_server(outside, tmp_path) as server:
        for route, content in [("podcast", audio.content), ("chapters", chapters.content),
                               ("transcript", transcript.content)]:
            answer = requests.get(f"{server}/{route}", params=published)
            assert answer.status_code == 200
            assert answer.content == content
        assert outside.counts() == hits
