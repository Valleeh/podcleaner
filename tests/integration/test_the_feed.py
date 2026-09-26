"""What a podcatcher gets when it subscribes.

Nothing here plays an episode.

The promise under test is `docs/requirements.md`, "Subscribing": the feed returned is the
publisher's with links changed and nothing else.  That is checked by putting the links
back and comparing, rather than by listing the tags that must survive -- a list would have
to grow every time a publisher uses a tag nobody here thought of, which is exactly how v1
silently dropped every itunes tag, image and category.
"""

from __future__ import annotations

import requests
from lxml import etree
from urllib.parse import parse_qs, urlparse

from tests.integration.support import podclean_server

PODCAST_NS = "https://podcastindex.org/namespace/1.0"
GUID = "d25942ec-d60e-11f0-a305-d75fbc6ff430"

#: A feed with the things a publisher actually ships and this server must not touch:
#: a CDATA body with markup in it, itunes tags, an image, a category, an atom link, and
#: a chapter link of the publisher's own.
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:podcast="{ns}">
<channel>
  <title>Hacks On Tap</title>
  <link>https://hacksontap.example/</link>
  <atom:link href="https://hacksontap.example/feed.xml" rel="self" type="application/rss+xml"/>
  <itunes:author>David Axelrod</itunes:author>
  <itunes:image href="https://hacksontap.example/art.jpg"/>
  <itunes:category text="News"><itunes:category text="Politics"/></itunes:category>
  <itunes:explicit>false</itunes:explicit>
  <item>
    <title>Oh Canada! (With Jonathan Martin)</title>
    <description><![CDATA[<p>Axe &amp; co. on <b>Canada</b>. Tickets at <a href="https://x.example">x.example</a>.</p>]]></description>
    <pubDate>Sun, 31 Aug 2026 09:00:00 -0400</pubDate>
    <guid isPermaLink="false">{guid}</guid>
    <itunes:duration>4380</itunes:duration>
    <enclosure url="{origin}/episode.mp3" type="audio/mpeg" length="87921872"/>
    <podcast:chapters url="{origin}/chapters.json" type="application/json+chapters"/>
  </item>
</channel>
</rss>"""

PLAIN_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel><title>No Namespace Here</title>
<item><title>An episode</title><guid isPermaLink="false">{guid}</guid>
<enclosure url="{origin}/episode.mp3" type="audio/mpeg"/></item>
</channel>
</rss>"""

#: Two items sharing an identifier, beside one ordinary episode.  The ordinary one is not
#: decoration: a feed in which *every* item collides has nothing bindable in it at all,
#: and this server answers the whole request 502 rather than returning the document
#: untouched.  That is a real feed's shape, and the promise is about the colliding pair.
TWINS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="{ns}">
<channel><title>Twins</title>
<item><title>An ordinary episode</title><guid isPermaLink="false">only-me</guid>
<enclosure url="{origin}/ordinary.mp3" type="audio/mpeg"/></item>
<item><title>First</title><guid isPermaLink="false">{guid}</guid>
<enclosure url="{origin}/first.mp3" type="audio/mpeg"/></item>
<item><title>Second, same id</title><guid isPermaLink="false">{guid}</guid>
<enclosure url="{origin}/second.mp3" type="audio/mpeg"/></item>
</channel>
</rss>"""

#: A guid written as CDATA.  Megaphone writes every one of them that way, so this is not
#: an exotic shape: the markers belong to the document, not to the publisher's name for
#: the episode.
CDATA_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel><title>Wrapped Identifiers</title>
<item><title>An episode</title><guid isPermaLink="false"><![CDATA[{guid}]]></guid>
<enclosure url="{origin}/episode.mp3" type="audio/mpeg"/></item>
</channel>
</rss>"""

_PARSER = etree.XMLParser(strip_cdata=False, resolve_entities=False)


def _shape_without_links(xml_text: str):
    """Everything the document says, with the links taken out of the comparison.

    Every ``url`` attribute is blanked and every transcript element is dropped, on both
    sides, so what is left is the publisher's content: which elements exist, in which
    order, with what text and what other attributes.  Whitespace between elements is not
    content and is deliberately not compared.
    """
    root = etree.fromstring(xml_text.encode("utf-8"), _PARSER)
    for element in list(root.iter(f"{{{PODCAST_NS}}}transcript")):
        element.getparent().remove(element)
    for element in root.iter():
        if "url" in element.attrib:
            element.set("url", "")
    return [(element.tag, (element.text or "").strip(), tuple(sorted(element.attrib.items())))
            for element in root.iter()]


def _enclosure_url(xml_text: str) -> str:
    """The audio link a podcatcher would follow."""
    return etree.fromstring(xml_text.encode("utf-8"), _PARSER).find(".//enclosure").get("url")


def _rss(outside, tmp_path, feed_xml: str) -> tuple[str, str]:
    outside.serves("/feed.xml", "application/rss+xml", feed_xml)
    feed = f"{outside.url}/feed.xml"
    with podclean_server(outside, tmp_path) as podclean:
        return requests.get(f"{podclean}/rss", params={"feed": feed}).text, podclean


def test_the_feed_is_the_publishers_with_only_the_links_changed(outside, tmp_path):
    original = FEED.format(ns=PODCAST_NS, guid=GUID, origin=outside.url)
    rewritten, podclean = _rss(outside, tmp_path, original)

    # The links do point here, or the comparison below would pass on a server that
    # returned the publisher's document untouched and served nothing.
    assert f"{podclean}/podcast?" in rewritten
    assert f"{podclean}/chapters?" in rewritten
    assert f"{podclean}/transcript?" in rewritten

    assert _shape_without_links(rewritten) == _shape_without_links(original)


def test_a_feed_without_the_podcast_namespace_keeps_exactly_what_it_had(outside, tmp_path):
    original = PLAIN_FEED.format(guid=GUID, origin=outside.url)
    rewritten, podclean = _rss(outside, tmp_path, original)

    # The audio link is this server's business and is repointed. The two sidecars are not
    # added, because declaring a namespace in someone's document is a bigger change than
    # this promises.
    assert f"{podclean}/podcast?" in rewritten
    assert "chapters" not in rewritten
    assert "transcript" not in rewritten
    assert _shape_without_links(rewritten) == _shape_without_links(original)


def test_a_guid_wrapped_in_cdata_names_the_episode_the_publisher_named(outside, tmp_path):
    original = CDATA_FEED.format(guid=GUID, origin=outside.url)
    rewritten, _ = _rss(outside, tmp_path, original)

    # Carrying the markers into the link would still play: the server would look the
    # episode up under the same mangled name it stored it under. What breaks is everything
    # outside it -- `./run verify` addressing the episode by the guid the publisher
    # published, and a listener reading the URL their podcatcher followed.
    query = parse_qs(urlparse(_enclosure_url(rewritten)).query)
    assert query["guid"] == [GUID]

    # And the document keeps its own CDATA: the markers are not this server's to remove.
    assert _shape_without_links(rewritten) == _shape_without_links(original)


def test_two_episodes_sharing_an_identifier_keep_the_publishers_link(outside, tmp_path):
    original = TWINS_FEED.format(ns=PODCAST_NS, guid=GUID, origin=outside.url)
    rewritten, podclean = _rss(outside, tmp_path, original)

    # A link that resolves to the wrong episode is worse than no link, and this server
    # cannot tell these two apart. Both keep what the publisher wrote, while the episode
    # it *can* address is repointed as usual.
    assert f"{outside.url}/first.mp3" in rewritten
    assert f"{outside.url}/second.mp3" in rewritten
    assert f"{podclean}/podcast?" in rewritten
    assert f"{outside.url}/ordinary.mp3" not in rewritten
