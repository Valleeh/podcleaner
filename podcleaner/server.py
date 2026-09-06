"""Serves the rewritten RSS feed and the audio a podcatcher plays.

Two routes only:

``GET /rss?feed=<origin feed url>``
    Fetch the origin feed and rewrite its enclosure URLs to point back here.  Cheap
    enough to redo on every request -- nothing about the feed itself is cached.  Also
    records each item's (feed, guid) -> origin-enclosure-URL binding
    (:mod:`podcleaner.store`), which is the only thing ``/podcast`` below will ever fetch.

``GET /podcast?feed=<origin feed url>&guid=<episode guid>``
    Serve that episode's audio, fetching it first if this is the first request for it.
    This route blocks the caller until the file exists: there is no polling, no job id,
    no status endpoint, because the MVP serves one listener and a podcatcher's own GET
    already waits for the response.  The URL fetched is looked up from (feed, guid),
    never taken from the caller: cut-guard's step-2 review -- guids are public (they are
    in the origin RSS), so a ``/podcast?url=...&guid=<real guid>`` that took the
    enclosure URL from the caller would let anyone permanently pin arbitrary bytes to a
    real episode.  Both ``feed`` and ``guid`` are part of the identity, not just the
    guid, because a guid is only unique within its own feed -- see
    :mod:`podcleaner.store`'s docstring for the collision that prevents, and for exactly
    what this does and does not defend against (it narrows the arbitrary-host fetch to a
    caller-hosted feed of the caller's own choosing; it does not add authentication).  A
    (feed, guid) pair this server has never seen named in a feed it fetched is a 404, not
    a fetch.

No detection or cutting runs from here (docs/mvp.md step 2): whatever
:mod:`podcleaner.fetch` returns -- the ad-free master when its shortcut verified, the
podcatcher variant otherwise -- is exactly what gets served.
"""

from __future__ import annotations

import os
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.util import FileWrapper

from podcleaner import fetch, feed, store
from podcleaner.logging import configure_logging, get_logger

__all__ = ["app", "run"]

logger = get_logger(__name__)

HOST = os.environ.get("PODCLEANER_HOST", "0.0.0.0")
PORT = int(os.environ.get("PODCLEANER_PORT", "8080"))
BASE_URL = os.environ.get("PODCLEANER_BASE_URL", f"http://localhost:{PORT}")

_CHUNK = 1 << 16


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _text(status: str, body: str, start_response, content_type: str = "text/plain; charset=utf-8"):
    payload = body.encode("utf-8")
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(payload)))])
    return [payload]


def _handle_rss(query, start_response):
    feed_url = query.get("feed", [None])[0]
    if not feed_url:
        return _text("400 Bad Request", "missing ?feed=\n", start_response)
    try:
        xml = feed.fetch_feed(feed_url)
        result = feed.patch_enclosures(xml, f"{BASE_URL}/podcast", feed_url)
    except feed.FeedError as exc:
        logger.warning("rss_failed", feed=feed_url, error=str(exc))
        return _text("502 Bad Gateway", f"{exc}\n", start_response)
    for guid, origin_url in result.bindings.items():
        store.record_source(feed_url, guid, origin_url)
    start_response(
        "200 OK",
        [("Content-Type", "application/rss+xml; charset=utf-8"), ("Content-Length", str(len(result.xml)))],
    )
    return [result.xml]


def _handle_podcast(query, start_response):
    guid = query.get("guid", [None])[0]
    feed_url = query.get("feed", [None])[0]
    if not guid or not feed_url:
        return _text("400 Bad Request", "missing ?guid= or ?feed=\n", start_response)

    try:
        url = store.read_source(feed_url, guid)
    except store.StoreError as exc:
        return _text("400 Bad Request", f"{exc}\n", start_response)
    if url is None:
        return _text(
            "404 Not Found", "unknown episode: no feed fetched by this server has named this (feed, guid)\n", start_response
        )

    final_path = store.episode_path(feed_url, guid)  # already validated by read_source above

    # Not invalidated automatically: if the origin later renames this same guid's URL
    # (a takedown, a re-edit), this cache keeps serving the file fetched here forever.
    # See store.py's docstring for why that can't be told apart from ordinary per-request
    # URL variance, and for the manual recovery. Deliberately not handled (docs/mvp.md).
    if not final_path.exists():
        with store.episode_lock(feed_url, guid):
            if not final_path.exists():  # someone else may have finished while we waited
                final_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = final_path.with_suffix(final_path.suffix + ".part")
                try:
                    result = fetch.fetch_episode(url, tmp_path)
                except fetch.FetchError as exc:
                    # fetch.fetch_episode (via _download) already removes tmp_path itself
                    # on any refusal -- that invariant lives with the code that creates
                    # the file, not with each of its callers.
                    logger.warning("podcast_fetch_failed", url=url, guid=guid, error=str(exc))
                    return _text("502 Bad Gateway", f"{exc}\n", start_response)
                store.write_meta(
                    feed_url,
                    guid,
                    {
                        "used_shortcut": result.used_shortcut,
                        "user_agent": result.user_agent,
                        "bytes_written": result.bytes_written,
                    },
                )
                store.publish(tmp_path, final_path)

    size = final_path.stat().st_size
    start_response("200 OK", [("Content-Type", "audio/mpeg"), ("Content-Length", str(size))])
    return FileWrapper(final_path.open("rb"), _CHUNK)


def app(environ, start_response):
    path = urlparse(environ.get("PATH_INFO", "")).path
    query = parse_qs(environ.get("QUERY_STRING", ""))
    if path == "/rss":
        return _handle_rss(query, start_response)
    if path == "/podcast":
        return _handle_podcast(query, start_response)
    return _text("404 Not Found", "no such route\n", start_response)


def run(host: str = HOST, port: int = PORT) -> None:
    configure_logging()
    httpd = make_server(host, port, app, server_class=_ThreadingWSGIServer, handler_class=WSGIRequestHandler)
    logger.info("server_start", host=host, port=port, base_url=BASE_URL)
    httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    run()
