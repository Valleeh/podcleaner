"""Offline tests for podcleaner.server: routing, the fetch-once-serve-many contract, and
the (feed, guid) -> URL binding that keeps /podcast from fetching whatever a caller names.

fetch.py and feed.py and store.py each have their own tests for their own logic; what's
worth checking here is the wiring in server.py that ties them together -- specifically
that an already-published episode is served without ever touching fetch (architecture.md:
"second play of a processed episode... without touching registry, worker or lock"), that
two concurrent first requests for the same brand-new episode still only fetch once, that
/podcast only ever fetches a URL /rss already recorded for that (feed, guid) pair -- never
one a caller supplies, and never a pair nobody has ever named -- and (cut-guard's second
step-2 review) that two different feeds sharing a guid never collide end to end.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from podcleaner import server

FEED_URL = "https://origin.test/feed.xml"
OTHER_FEED_URL = "https://origin.test/other-feed.xml"


def _environ(path: str, query: str = "") -> dict:
    return {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": "GET"}


def _call(environ):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(server.app(environ, start_response))
    return captured["status"], captured["headers"], body


def test_unknown_route_is_404():
    status, _, _ = _call(_environ("/nope"))
    assert status.startswith("404")


def test_rss_requires_feed_param():
    status, _, _ = _call(_environ("/rss"))
    assert status.startswith("400")


def test_rss_returns_the_patched_feed_and_records_its_bindings(tmp_path, monkeypatch):
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(server.feed, "fetch_feed", lambda url: b"<rss>raw</rss>")
    monkeypatch.setattr(
        server.feed,
        "patch_enclosures",
        lambda xml, base, feed_url: server.feed.FeedPatchResult(
            xml=b"<rss>patched</rss>", bindings={"guid-1": "https://origin.test/ep1.mp3"}
        ),
    )

    status, headers, body = _call(_environ("/rss", f"feed={FEED_URL}"))

    assert status.startswith("200")
    assert body == b"<rss>patched</rss>"
    assert headers["Content-Type"].startswith("application/rss+xml")
    assert server.store.read_source(FEED_URL, "guid-1") == "https://origin.test/ep1.mp3"


def test_rss_reports_upstream_failure_as_502(monkeypatch):
    def raise_it(url):
        raise server.feed.FeedError("origin is down")

    monkeypatch.setattr(server.feed, "fetch_feed", raise_it)

    status, _, _ = _call(_environ("/rss", f"feed={FEED_URL}"))
    assert status.startswith("502")


def test_podcast_requires_guid_and_feed():
    status, _, _ = _call(_environ("/podcast"))
    assert status.startswith("400")
    status, _, _ = _call(_environ("/podcast", "guid=some-guid"))  # feed= missing
    assert status.startswith("400")


def test_podcast_404s_for_a_pair_no_feed_ever_named(tmp_path, monkeypatch):
    """The fix for cut-guard's step-2 review: a (feed, guid) pair this server has never
    seen bound to a URL (by /rss) is unknown, not fetchable."""
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)

    def must_not_be_called(*a, **kw):
        raise AssertionError("fetch_episode must not run for a pair with no recorded source")

    monkeypatch.setattr(server.fetch, "fetch_episode", must_not_be_called)

    status, _, _ = _call(_environ("/podcast", f"guid=nobody-has-ever-named-this&feed={FEED_URL}"))
    assert status.startswith("404")


def test_podcast_ignores_a_caller_supplied_url_and_uses_only_the_recorded_source(tmp_path, monkeypatch):
    """The attack cut-guard's step-2 review describes: a caller who knows a real
    (public) guid supplies their own ?url=. It must be ignored entirely -- only the URL
    /rss itself recorded for that (feed, guid) pair is ever fetched."""
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "real-guid", "https://origin.test/the-real-episode.mp3")

    fetched_urls = []

    def fake_fetch(url, dest):
        fetched_urls.append(url)
        Path(dest).write_bytes(b"the real episode")
        return server.fetch.FetchResult(Path(dest), True, "curl/7.88.1", 17)

    monkeypatch.setattr(server.fetch, "fetch_episode", fake_fetch)

    status, _, body = _call(
        _environ("/podcast", f"url=https://attacker.test/10-seconds.mp3&guid=real-guid&feed={FEED_URL}")
    )

    assert status.startswith("200")
    assert fetched_urls == ["https://origin.test/the-real-episode.mp3"]
    assert body == b"the real episode"


def test_podcast_serves_an_already_published_episode_without_fetching(tmp_path, monkeypatch):
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "cached-guid", "https://origin.test/ep.mp3")
    final = server.store.episode_path(FEED_URL, "cached-guid")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"already here, no fetch needed")

    def must_not_be_called(*a, **kw):
        raise AssertionError("fetch_episode must not run for an already-published episode")

    monkeypatch.setattr(server.fetch, "fetch_episode", must_not_be_called)

    status, headers, body = _call(_environ("/podcast", f"guid=cached-guid&feed={FEED_URL}"))

    assert status.startswith("200")
    assert body == b"already here, no fetch needed"
    assert headers["Content-Type"] == "audio/mpeg"


def test_podcast_fetches_publishes_and_records_provenance_for_a_new_episode(tmp_path, monkeypatch):
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "new-guid", "https://origin.test/new.mp3")
    calls = []

    def fake_fetch(url, dest):
        calls.append(url)
        Path(dest).write_bytes(b"freshly fetched bytes")
        return server.fetch.FetchResult(Path(dest), True, "curl/7.88.1", 22)

    monkeypatch.setattr(server.fetch, "fetch_episode", fake_fetch)

    status, headers, body = _call(_environ("/podcast", f"guid=new-guid&feed={FEED_URL}"))

    assert status.startswith("200")
    assert body == b"freshly fetched bytes"
    assert calls == ["https://origin.test/new.mp3"]
    final = server.store.episode_path(FEED_URL, "new-guid")
    assert final.exists() and final.read_bytes() == b"freshly fetched bytes"
    # docs/mvp.md step 4 needs this to decide whether detection has to run at all
    assert server.store.read_meta(FEED_URL, "new-guid") == {
        "used_shortcut": True,
        "user_agent": "curl/7.88.1",
        "bytes_written": 22,
    }


def test_podcast_upstream_fetch_failure_is_502_and_leaves_no_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "bad-guid", "https://origin.test/bad.mp3")

    def fake_fetch(url, dest):
        raise server.fetch.FetchError("origin refused the connection")

    monkeypatch.setattr(server.fetch, "fetch_episode", fake_fetch)

    status, _, _ = _call(_environ("/podcast", f"guid=bad-guid&feed={FEED_URL}"))

    assert status.startswith("502")
    final = server.store.episode_path(FEED_URL, "bad-guid")
    assert not final.exists()
    assert not final.with_suffix(final.suffix + ".part").exists()
    assert server.store.read_meta(FEED_URL, "bad-guid") is None


def test_podcast_concurrent_requests_for_the_same_new_episode_fetch_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "co-guid", "https://origin.test/co.mp3")
    calls = []
    call_guard = threading.Lock()
    fetch_started = threading.Event()

    def fake_fetch(url, dest):
        with call_guard:
            calls.append(url)
        fetch_started.set()
        time.sleep(0.15)
        Path(dest).write_bytes(b"the one true fetch")
        return server.fetch.FetchResult(Path(dest), True, "curl/7.88.1", 19)

    monkeypatch.setattr(server.fetch, "fetch_episode", fake_fetch)

    results = []

    def worker():
        status, _, body = _call(_environ("/podcast", f"guid=co-guid&feed={FEED_URL}"))
        results.append((status, body))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    fetch_started.wait(timeout=2)  # let t1 get inside fetch (and the lock) before t2 starts
    t2.start()
    t1.join()
    t2.join()

    assert calls == ["https://origin.test/co.mp3"]
    assert all(status.startswith("200") for status, _ in results)
    assert all(body == b"the one true fetch" for _, body in results)


def test_podcast_does_not_collide_when_two_feeds_share_a_guid(tmp_path, monkeypatch):
    """End-to-end version of cut-guard's finding: show A and show B both use guid
    "12345". Fetching A's episode must never serve B's, or the reverse, even though the
    guid alone is identical."""
    monkeypatch.setattr(server.store, "STORE_ROOT", tmp_path)
    server.store.record_source(FEED_URL, "12345", "https://origin.test/showA-episode.mp3")
    server.store.record_source(OTHER_FEED_URL, "12345", "https://origin.test/showB-episode.mp3")

    def fake_fetch(url, dest):
        content = b"SHOW A AUDIO" if "showA" in url else b"SHOW B AUDIO"
        Path(dest).write_bytes(content)
        return server.fetch.FetchResult(Path(dest), True, "curl/7.88.1", len(content))

    monkeypatch.setattr(server.fetch, "fetch_episode", fake_fetch)

    status_a, _, body_a = _call(_environ("/podcast", f"guid=12345&feed={FEED_URL}"))
    status_b, _, body_b = _call(_environ("/podcast", f"guid=12345&feed={OTHER_FEED_URL}"))

    assert status_a.startswith("200") and body_a == b"SHOW A AUDIO"
    assert status_b.startswith("200") and body_b == b"SHOW B AUDIO"

    # and re-requesting A afterwards still serves A's cached file, not B's
    status_a2, _, body_a2 = _call(_environ("/podcast", f"guid=12345&feed={FEED_URL}"))
    assert status_a2.startswith("200") and body_a2 == b"SHOW A AUDIO"
