"""Offline tests for podcleaner.feed: in-place enclosure rewriting and the guid -> URL
bindings it hands back.

The point of doing this with lxml instead of parsing into a model and regenerating XML
(what v1 did) is that everything *not* named ``enclosure/@url`` survives untouched --
itunes tags, images, categories, CDATA bodies.  That claim is exactly what the first
test below checks; it is the case that would have caught the v1 mistake.

The rewritten enclosure carries the guid and the feed URL, never the origin enclosure URL
(cut-guard's step-2 review: server.py must never let a caller name the URL it fetches),
so what proves the original URL is preserved at all is the returned ``bindings`` mapping.
The feed URL is part of the rewritten link (not just the guid) because a guid is only
unique within its own feed -- see podcleaner/store.py's docstring for the collision that
prevents.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import requests
from lxml import etree

from podcleaner import feed

FEED_URL = "https://origin.test/feed.xml"

_FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>Test Podcast</title>
<link>https://example.test/</link>
<description><![CDATA[A <b>test</b> feed with HTML in its description.]]></description>
<itunes:image href="https://example.test/art.jpg"/>
<itunes:category text="News"/>
<itunes:owner><itunes:email>owner@example.test</itunes:email></itunes:owner>
<atom:link rel="self" href="https://example.test/feed.xml"/>
{items}
</channel>
</rss>
"""


def _item(guid: str, enclosure_url: str, *, has_guid: bool = True) -> str:
    guid_xml = f'<guid isPermaLink="false">{guid}</guid>' if has_guid else ""
    return f"""<item>
<title>Episode</title>
{guid_xml}
<enclosure url="{enclosure_url}" length="123" type="audio/mpeg"/>
<itunes:duration>01:00:00</itunes:duration>
</item>"""


def _feed(items: str) -> bytes:
    return _FEED_TEMPLATE.format(items=items).encode("utf-8")


def test_patch_rewrites_enclosure_and_preserves_everything_else():
    # "&" must be escaped in the XML source itself, same as a real feed does (see the
    # live Lage der Nation enclosure: "...?ptm_source=feed&amp;ptm_context=mp3...").
    xml = _feed(_item("guid-1", "https://origin.test/ep1.mp3?tok=abc&amp;x=1"))

    result = feed.patch_enclosures(xml, "http://localhost:8080/podcast", FEED_URL)

    # untouched: itunes tags, image, category, owner, CDATA body, enclosure/@length
    assert b"<![CDATA[A <b>test</b> feed with HTML in its description.]]>" in result.xml
    assert b'itunes:image href="https://example.test/art.jpg"' in result.xml
    assert b'itunes:category text="News"' in result.xml
    assert b"<itunes:email>owner@example.test</itunes:email>" in result.xml
    assert b"<itunes:duration>01:00:00</itunes:duration>" in result.xml
    assert b'length="123"' in result.xml

    root = etree.fromstring(result.xml)
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    enclosure = root.find(".//item/enclosure")
    parsed = urlparse(enclosure.get("url"))
    assert parsed.path == "/podcast"
    qs = parse_qs(parsed.query)
    assert qs["guid"] == ["guid-1"]
    assert qs["feed"] == [FEED_URL]
    assert "url" not in qs  # the caller must never be able to name the fetched URL

    # the original URL is only available via the bindings the caller records itself
    assert result.bindings == {"guid-1": "https://origin.test/ep1.mp3?tok=abc&x=1"}
    # confirm itunes elements still parse under their namespace, not just as raw bytes
    assert root.find(".//itunes:category", namespaces=ns) is not None


def test_patch_rejects_malformed_xml():
    with pytest.raises(feed.FeedError):
        feed.patch_enclosures(b"<rss><channel><item>not closed", "http://localhost:8080/podcast", FEED_URL)


def test_patch_skips_items_without_a_guid_but_keeps_rewriting_others():
    items = _item("guid-2", "https://origin.test/ep2.mp3") + _item(
        "", "https://origin.test/ep-no-guid.mp3", has_guid=False
    )
    result = feed.patch_enclosures(_feed(items), "http://localhost:8080/podcast", FEED_URL)

    root = etree.fromstring(result.xml)
    urls = [it.find("enclosure").get("url") for it in root.iter("item")]
    assert urls[0].startswith("http://localhost:8080/podcast?")
    assert urls[1] == "https://origin.test/ep-no-guid.mp3"  # no guid to key on: left alone
    assert result.bindings == {"guid-2": "https://origin.test/ep2.mp3"}


def test_patch_raises_when_no_item_has_both_enclosure_and_guid():
    xml = _feed(_item("", "https://origin.test/ep.mp3", has_guid=False))
    with pytest.raises(feed.FeedError):
        feed.patch_enclosures(xml, "http://localhost:8080/podcast", FEED_URL)


def test_fetch_feed_raises_on_http_failure(monkeypatch):
    def fake_get(url, timeout):
        raise requests.ConnectionError("dns failure")

    monkeypatch.setattr(feed.requests, "get", fake_get)
    with pytest.raises(feed.FeedError):
        feed.fetch_feed("https://origin.test/feed.xml")
