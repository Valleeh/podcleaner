"""What the routes answer when there is nothing to serve yet, and when the publisher breaks.

Needs no audio and no fixtures.  Every promise here is about a route refusing to do work:
the two sidecars describe audio that does not exist yet, an episode nobody named cannot be
fetched, and a publisher that fails must not leave anything frozen behind.

The point of each is the same and it is a cost promise as much as a correctness one: a
route a crawler can reach must never start a paid, minutes-long pipeline.
"""

from __future__ import annotations

import requests

from tests.integration.support import podclean_server

GUID = "d25942ec-d60e-11f0-a305-d75fbc6ff430"

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Hacks On Tap</title>
<item><title>Oh Canada!</title><guid isPermaLink="false">{guid}</guid>
<enclosure url="{origin}/episode.mp3" type="audio/mpeg"/></item>
</channel>
</rss>"""


def test_the_sidecars_say_nothing_until_the_episode_has_been_played(outside, tmp_path):
    outside.serves("/feed.xml", "application/rss+xml", FEED.format(guid=GUID, origin=outside.url))
    feed = f"{outside.url}/feed.xml"
    episode = {"feed": feed, "guid": GUID}

    with podclean_server(outside, tmp_path) as podclean:
        # Subscribing is enough for the server to know this episode exists.
        assert requests.get(f"{podclean}/rss", params={"feed": feed}).status_code == 200

        for route in ("chapters", "transcript"):
            answer = requests.get(f"{podclean}/{route}", params=episode)
            assert answer.status_code == 404, f"/{route} described audio that does not exist"

        # And neither of them started the work that would make the audio.  This is the
        # whole promise: a document that describes a cut cannot exist before the cut.
        assert "/episode.mp3" not in outside.counts()
        assert "/audio/transcriptions" not in outside.counts()
        assert "/chat/completions" not in outside.counts()


def test_an_episode_no_feed_has_named_is_refused_and_nothing_goes_out(outside, tmp_path):
    outside.serves("/feed.xml", "application/rss+xml", FEED.format(guid=GUID, origin=outside.url))

    with podclean_server(outside, tmp_path) as podclean:
        # No feed was ever fetched, so this pair has never been named to this server.
        answer = requests.get(f"{podclean}/podcast",
                              params={"feed": f"{outside.url}/feed.xml", "guid": GUID})
        assert answer.status_code == 404

        # Nobody was asked anything. A caller cannot make this server fetch a URL it was
        # handed rather than one it saw named in a feed it read itself.
        assert outside.counts() == {}


def test_a_publisher_that_fails_is_reported_and_nothing_is_kept(outside, tmp_path):
    outside.serves("/feed.xml", "application/rss+xml", FEED.format(guid=GUID, origin=outside.url))
    outside.fails("/episode.mp3")
    feed = f"{outside.url}/feed.xml"
    episode = {"feed": feed, "guid": GUID}

    with podclean_server(outside, tmp_path) as podclean:
        requests.get(f"{podclean}/rss", params={"feed": feed})

        assert requests.get(f"{podclean}/podcast", params=episode).status_code == 502

        # Nothing was kept: no sidecars appeared, and asking again tries the publisher
        # again rather than serving or remembering a failure.
        assert requests.get(f"{podclean}/chapters", params=episode).status_code == 404
        assert requests.get(f"{podclean}/transcript", params=episode).status_code == 404
        assert requests.get(f"{podclean}/podcast", params=episode).status_code == 502
        assert outside.counts()["/episode.mp3"] == 2


def test_a_publisher_that_answers_with_something_that_is_not_audio_is_refused(outside, tmp_path):
    """A block page, 200, its own length, calling itself audio.

    v1 published one of these as ``audio.mp3`` and never looked again, because nothing
    re-examines an episode that has one.  The lesson written down at the time is that a
    blocklist of what a response *calls* itself cannot be completed -- so what is trusted
    here is the bytes, and this stub lies in the one way that matters.
    """
    outside.serves("/feed.xml", "application/rss+xml", FEED.format(guid=GUID, origin=outside.url))
    outside.serves("/episode.mp3", "audio/mpeg",
                   "<html><head><title>Attention Required!</title></head>"
                   "<body><h1>Sorry, you have been blocked</h1></body></html>")
    feed = f"{outside.url}/feed.xml"
    episode = {"feed": feed, "guid": GUID}

    with podclean_server(outside, tmp_path) as podclean:
        requests.get(f"{podclean}/rss", params={"feed": feed})

        assert requests.get(f"{podclean}/podcast", params=episode).status_code == 502

        # Nothing was kept, and nothing was paid for on the way: a page that is not audio
        # is not transcribed and not classified, and the next play asks the publisher
        # again rather than serving what it got.
        assert requests.get(f"{podclean}/chapters", params=episode).status_code == 404
        assert requests.get(f"{podclean}/transcript", params=episode).status_code == 404
        assert "/audio/transcriptions" not in outside.counts()
        assert "/chat/completions" not in outside.counts()
        assert requests.get(f"{podclean}/podcast", params=episode).status_code == 502
        assert outside.counts()["/episode.mp3"] == 2
