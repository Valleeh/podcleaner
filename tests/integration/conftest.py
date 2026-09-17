"""Fixtures shared by every file here.  The harness itself is in ``support.py``."""

from __future__ import annotations

import pytest

from tests.integration.episode import build
from tests.integration.support import Outside, model_reply


@pytest.fixture(scope="session")
def episode(tmp_path_factory):
    """The episode the audio tests play, built once for the whole run.

    Session-scoped because ffmpeg takes about fifteen seconds over it and no test alters
    it: the five that play one play the same one, as five listeners would.
    """
    return build(tmp_path_factory.mktemp("episode"))


@pytest.fixture
def outside():
    """The faked outside world, and afterwards: was it asked anything it did not know?

    The check is here rather than in each test so that it costs nothing to have. A server
    reaching for a path this suite never described is either a contract these tests have
    not written down or a request that should not be going out at all, and both are worth
    a failure rather than a silent 404.
    """
    stub = Outside()
    yield stub
    stub.httpd.shutdown()
    assert stub.unknown_paths() == [], "the server asked for paths this suite never described"


@pytest.fixture
def published(outside, episode):
    """An addressable episode with the publisher and paid endpoints ready to answer."""
    wanted = {"feed": f"{outside.url}/feed.xml", "guid": "test-episode"}
    outside.serves("/feed.xml", "application/rss+xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Hacks On Tap</title><item><title>Oh Canada!</title>
<guid>{wanted['guid']}</guid>
<enclosure url="{outside.url}/episode.mp3" type="audio/mpeg"/></item></channel></rss>""")
    outside.serves("/episode.mp3", "audio/mpeg", episode.mp3.read_bytes())
    outside.serves("/audio/transcriptions", "application/json", episode.transcript)
    outside.serves("/chat/completions", "application/json", model_reply(
        [b.as_segment() for b in episode.breaks],
        [{"cue": mark.cue, "title": mark.title} for mark in episode.chapters]))
    return wanted
