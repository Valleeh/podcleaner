"""Fetch the origin RSS feed and rewrite each item's enclosure URL to point back here.

Only ``enclosure/@url`` is touched.  Every other byte -- itunes tags, artwork,
categories, CDATA bodies, formatting -- passes through unchanged: the document is
edited in place with lxml, never rebuilt from a parsed model.  v1 did the latter
(parse into a dict, regenerate XML from it) and silently dropped the itunes tags,
images and categories; see docs/mvp.md, step 2.

The rewritten enclosure carries the episode's guid and the feed URL it came from, never
the origin enclosure URL itself.  cut-guard's step-2 review: if ``/podcast`` took the
enclosure URL from the caller, any caller who knew a real (public) guid could pin
arbitrary bytes to it permanently, before the real podcatcher ever asked.  So this module
hands back the guid -> origin-URL binding it just wrote into the feed as data
(:class:`FeedPatchResult.bindings`), and it is on the caller (``server.py``) to record it
-- ``/podcast`` then only ever fetches a URL this server itself already saw named, in the
feed it was named in, never one a caller supplies.  The feed URL is part of the identity,
not just the guid, because a guid is only unique within its own feed
(:mod:`podcleaner.store`'s docstring has the collision this prevents).

What this narrows the risk to, and does not eliminate: ``/rss?feed=`` has no allowlist on
which feed URLs it will fetch, so an unauthenticated caller can still point it at a feed
they host themselves and have this server fetch and cache whatever that document says,
under the (feed, guid) identity it declares.  That does not let them touch a *different*,
real feed's episodes -- this server would have to fetch that real feed's own URL and have
it return attacker content, which requires controlling that origin.  It is the same shape
as the already-accepted "no authentication" risk (`docs/mvp.md`, After the MVP list).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict
from urllib.parse import urlencode

import requests
from lxml import etree

from podcleaner.logging import get_logger

__all__ = ["FeedError", "FeedPatchResult", "fetch_feed", "patch_enclosures"]

logger = get_logger(__name__)

_TIMEOUT = 30


class FeedError(RuntimeError):
    """Raised when the origin feed cannot be fetched, is not well-formed, or has nothing
    that can be safely rewritten."""


@dataclass(frozen=True)
class FeedPatchResult:
    xml: bytes
    bindings: Dict[str, str] = field(default_factory=dict)  # guid -> original enclosure url


def fetch_feed(feed_url: str) -> bytes:
    """Fetch the origin feed's raw bytes, unparsed."""
    try:
        resp = requests.get(feed_url, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FeedError(f"GET {feed_url} failed: {exc}") from exc
    return resp.content


def patch_enclosures(xml_bytes: bytes, podcast_url: str, feed_url: str) -> FeedPatchResult:
    """Return ``xml_bytes`` with every item's ``enclosure/@url`` replaced by a link back
    to ``podcast_url`` carrying that item's guid and ``feed_url``, plus the guid ->
    original-url binding for every item rewritten.

    An item with an enclosure but no guid is left untouched: :mod:`podcleaner.store`
    keys an episode on its feed URL and guid together, and a wrong or missing key risks
    colliding two different episodes on disk, so a missing guid means refuse to rewrite
    *that item* rather than invent a key for it.  The whole feed only fails if nothing in
    it could be safely rewritten at all.

    ``enclosure/@length`` is left as the origin declared it, even when the shortcut will
    later serve a smaller file: this function runs before any episode is ever fetched, so
    it cannot know which variant a later ``/podcast`` request will end up serving, and
    rebuilding the value would mean guessing.  The gap this leaves is small (measured
    1-3%, see fetch.py) and the reference podcatcher for this project (AntennaPod) reads
    the actual HTTP ``Content-Length`` rather than trusting the feed; a stricter client
    could treat it as an interrupted download, which is a usability annoyance, not lost
    or wrong audio -- the one rule is about content, and no content is at stake here.
    """
    # A fresh parser per call: lxml parsers carry mutable parse-time state and are not
    # documented as safe to share across concurrent calls, and server.py serves /rss on
    # a threaded server where two requests can call this at once.  strip_cdata=False
    # preserves real feeds' CDATA descriptions (confirmed against the live Lage der
    # Nation feed, 2026-09-06) instead of re-escaping them as plain text -- a fidelity
    # break even though it parses the same.  resolve_entities=False avoids expanding
    # DTD-declared entities from a remote document.
    parser = etree.XMLParser(strip_cdata=False, resolve_entities=False)
    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FeedError(f"origin feed is not well-formed XML: {exc}") from exc

    bindings: Dict[str, str] = {}
    skipped = 0
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        original = enclosure.get("url") if enclosure is not None else None
        if not original:
            continue
        guid_el = item.find("guid")
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else None
        if not guid:
            skipped += 1
            logger.warning("feed_item_no_guid", enclosure=original)
            continue
        bindings[guid] = original
        query = urlencode({"guid": guid, "feed": feed_url})
        enclosure.set("url", f"{podcast_url}?{query}")

    if not bindings:
        raise FeedError("no item had both an enclosure and a guid; nothing to rewrite")
    logger.info("feed_patched", items=len(bindings), skipped_no_guid=skipped)

    encoding = root.getroottree().docinfo.encoding or "UTF-8"
    xml = etree.tostring(root, xml_declaration=True, encoding=encoding)
    return FeedPatchResult(xml=xml, bindings=bindings)
