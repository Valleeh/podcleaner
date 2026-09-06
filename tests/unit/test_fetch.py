"""Offline tests for podcleaner.fetch: the User-Agent shortcut decision, the size/content
band it must clear, and the byte-count verification every download must pass.

No network here -- ``requests.head``/``requests.get`` are monkeypatched.  The cases that
matter most (cut-guard's step-2 review) are the ones where something *smaller* than the
real episode is not the episode at all: a block page, an empty response, a truncated
stream with no declared length -- and the case where the number that justified trusting
a download doesn't match what actually got served.
"""

from __future__ import annotations

import pytest
import requests

from podcleaner import fetch
from podcleaner.fetch import HeadInfo


class _FakeResponse:
    def __init__(self, *, headers=None, content=b"", raise_exc=None, status_code=200):
        self.headers = headers or {}
        self._content = content
        self._raise_exc = raise_exc
        self.status_code = status_code

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _audio(length: int) -> HeadInfo:
    return HeadInfo(length=length, content_type="audio/mpeg")


def _html(length: int) -> HeadInfo:
    return HeadInfo(length=length, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------- shortcut_applies


# Exact byte counts measured 2026-09-06 from the pinned fixture pairs (var/fixtures/
# audio/), the same evidence the band in fetch.py is derived from.  This ties the code's
# constants directly to the measured reality rather than to a number picked by feel.
_REAL_MEASURED_PAIRS = [
    ("ldn490", 57977837, 59198300),
    ("ldn491", 66296547, 68484653),
    ("solved-life-path", 180247406, 182340754),
]


@pytest.mark.parametrize("name, plain_bytes, podcatcher_bytes", _REAL_MEASURED_PAIRS)
def test_shortcut_applies_to_every_real_measured_master(name, plain_bytes, podcatcher_bytes):
    assert fetch.shortcut_applies(_audio(plain_bytes), _audio(podcatcher_bytes)) is True


def test_shortcut_rejects_a_block_page_far_smaller_than_the_episode():
    """The likeliest false positive: a WAF/bot-manager challenge page served with 200,
    produced by exactly the User-Agent discrimination the shortcut is built on."""
    block_page = HeadInfo(length=8_000, content_type="text/html; charset=utf-8")
    real_episode = _audio(68_484_653)
    assert fetch.shortcut_applies(block_page, real_episode) is False


def test_shortcut_rejects_an_empty_response():
    assert fetch.shortcut_applies(_audio(0), _audio(68_484_653)) is False


def test_shortcut_rejects_a_trailer_sized_response():
    """~78 of 80 minutes gone: a preview/trailer clip served to an unrecognised client."""
    trailer = _audio(2 * 1024 * 1024)
    full_episode = _audio(80 * 60 * 128 * 1000 // 8)  # ~80 min at 128 kbps
    assert fetch.shortcut_applies(trailer, full_episode) is False


def test_shortcut_rejects_non_audio_content_type_even_within_the_size_band():
    in_band_but_html = _html(66_296_547)
    assert fetch.shortcut_applies(in_band_but_html, _audio(68_484_653)) is False


@pytest.mark.parametrize(
    "plain, podcatcher",
    [
        (_audio(68_484_653), _audio(68_484_653)),  # identical: no shortcut in effect
        (_audio(68_800_000), _audio(68_484_653)),  # plain bigger: not how ad-insertion works
        (_audio(68_450_000), _audio(68_484_653)),  # 0.05%: noise, not proof
        (None, _audio(68_484_653)),
        (_audio(66_296_547), None),
        (None, None),
    ],
)
def test_shortcut_applies_false_cases(plain, podcatcher):
    assert fetch.shortcut_applies(plain, podcatcher) is False


# ---------------------------------------------------------------- fetch_episode


def test_fetch_uses_shortcut_for_a_verified_master(tmp_path, monkeypatch):
    plain_bytes = b"ID3" + b"x" * 997  # 1000 bytes total, begins with real audio magic
    podcatcher_bytes = b"x" * 1030  # 2.9% larger: inside the measured band

    def fake_head(url, *, headers, allow_redirects, timeout):
        ua = headers["User-Agent"]
        n = len(plain_bytes) if ua == fetch.PLAIN_USER_AGENT else len(podcatcher_bytes)
        return _FakeResponse(headers={"Content-Length": str(n), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        content = plain_bytes if headers["User-Agent"] == fetch.PLAIN_USER_AGENT else podcatcher_bytes
        return _FakeResponse(content=content, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(content))})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is True
    assert result.user_agent == fetch.PLAIN_USER_AGENT
    assert result.bytes_written == len(plain_bytes)
    assert dest.read_bytes() == plain_bytes


def test_fetch_falls_back_to_podcatcher_when_plain_is_a_block_page(tmp_path, monkeypatch):
    """A HEAD-only view of the failure mode issue 1 describes: the plain UA gets an HTML
    challenge page, tiny compared to the real episode.  The GET must never be trusted to
    publish that page as the episode; the podcatcher variant is fetched instead."""
    block_page = b"<html>are you a robot?</html>"
    real_episode = b"ID3" + b"y" * 1_999_997  # begins with real audio magic

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            return _FakeResponse(headers={"Content-Length": str(len(block_page)), "Content-Type": "text/html"})
        return _FakeResponse(headers={"Content-Length": str(len(real_episode)), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT, "must never GET the block page as if it were the episode"
        return _FakeResponse(
            content=real_episode, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(real_episode))}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is False
    assert dest.read_bytes() == real_episode


def test_fetch_falls_back_when_plain_head_is_an_empty_200(tmp_path, monkeypatch):
    real_episode = b"ID3" + b"z" * 499_997  # begins with real audio magic

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            return _FakeResponse(headers={"Content-Length": "0", "Content-Type": "audio/mpeg"})
        return _FakeResponse(headers={"Content-Length": str(len(real_episode)), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        return _FakeResponse(
            content=real_episode, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(real_episode))}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is False
    assert dest.read_bytes() == real_episode


def test_fetch_refuses_a_stream_that_ends_early_with_no_declared_length(tmp_path, monkeypatch):
    """The gap cut-guard's review names directly: a 2xx with no Content-Length and no
    chunked framing lets iter_content just stop, with nothing raised by requests itself.
    The only thing that can catch it is checking the byte count against what an earlier
    HEAD measured -- so this must be refused, not published."""
    full_length = 1_000_000

    def fake_head(url, *, headers, allow_redirects, timeout):
        # equal sizes both sides: no shortcut, exercise the podcatcher/fallback path
        return _FakeResponse(headers={"Content-Length": str(full_length), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        # no Content-Length header at all, and far fewer bytes than the HEAD measured
        return _FakeResponse(content=b"a" * 1000, headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="wrote 1000 bytes, expected 1000000"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    # F6: written != target fires after the file is fully written, and used to be the one
    # refusal _download did not clean up after itself -- left to whichever caller
    # remembered to.
    assert not dest.exists()


def test_fetch_refuses_when_get_length_disagrees_with_the_head_that_justified_it(tmp_path, monkeypatch):
    """The core of issue 2: the number that authorised trusting this fetch has to belong
    to the bytes that get served.  A GET declaring a different Content-Length than the
    HEAD that justified it is refused outright, even though the GET's own header is
    internally consistent with what it would deliver.

    ``plain_bytes`` begins with real audio magic (``ID3``) precisely so the byte-sniff
    cannot be what raises here: with the disagreement guard removed, the GET's declared
    length would simply be ignored, `written == target` would hold at 1000, the sniff
    would pass, and no error at all would be raised -- so this only stays green because
    of the guard it names, not because of the sniff catching a non-audio payload as a
    coincidental substitute."""
    plain_bytes = b"ID3" + b"m" * 997  # 1000 bytes total, begins with real audio magic
    podcatcher_bytes = b"m" * 1030

    def fake_head(url, *, headers, allow_redirects, timeout):
        ua = headers["User-Agent"]
        n = len(plain_bytes) if ua == fetch.PLAIN_USER_AGENT else len(podcatcher_bytes)
        return _FakeResponse(headers={"Content-Length": str(n), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        # declares a different length than the HEAD that justified using the plain UA
        return _FakeResponse(
            content=plain_bytes, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(plain_bytes) + 5)}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="earlier HEAD that justified this fetch measured"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_refuses_a_non_audio_get_response_on_the_shortcut_path(tmp_path, monkeypatch):
    """Defense in depth beyond the plain-UA HEAD content-type check: a HEAD and a GET
    are two different requests, so even after shortcut_applies passed on the HEAD's
    say-so, the actual GET response for the plain UA is checked again before it is
    trusted as the verified master."""
    plain_bytes = b"x" * 1000
    podcatcher_bytes = b"x" * 1030  # 2.9%: inside the band, so the shortcut is attempted

    def fake_head(url, *, headers, allow_redirects, timeout):
        n = len(plain_bytes) if headers["User-Agent"] == fetch.PLAIN_USER_AGENT else len(podcatcher_bytes)
        return _FakeResponse(headers={"Content-Length": str(n), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PLAIN_USER_AGENT
        # Not text/html: that is now refused unconditionally (see the regression test
        # below), so this must be merely "not audio" to exercise the shortcut-only
        # require_audio_content_type check on its own.
        return _FakeResponse(content=b'{"blocked": true}', headers={"Content-Type": "application/json"})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="not audio"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)


def test_fetch_tolerates_non_audio_content_type_on_the_fallback_path(tmp_path, monkeypatch):
    """cut-guard's step-2 review: the fallback path makes no claim beyond "this is what
    the podcatcher would get", so a publisher serving mp3s as application/octet-stream
    (an S3 bucket with no configured type is a common cause) must still get the full,
    honest, ad-laden download -- not a hard failure that leaves the listener no episode
    at all where before they had the complete one."""
    real_episode = b"ID3" + b"z" * 499_997  # begins with real audio magic

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            # shortcut unverifiable for a different reason (HEAD unsupported here);
            # what this test is actually about is the GET's content type below
            raise requests.ConnectionError("HEAD unsupported for this UA")
        return _FakeResponse(headers={"Content-Length": str(len(real_episode)), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(
            content=real_episode,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(real_episode))},
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is False
    assert dest.read_bytes() == real_episode


def test_fetch_refuses_a_self_consistent_html_block_page_on_the_fallback_path(tmp_path, monkeypatch):
    """The exact hostile edge from issue 1: both HEAD probes are unusable (refused here;
    a 405 or a HEAD with no Content-Length are the same condition for _head_probe), so
    there is no earlier-measured length to check the GET against, and the block page's
    own Content-Length matches its own body -- so the byte-count check in _download
    cannot tell it apart from the real episode.  Only the byte-sniff can (a Content-Type
    blocklist can't be completed -- see the module docstring), and it must fire on the
    fallback path too, or an HTML challenge page gets published as the episode and,
    since episode_path then exists, served forever."""
    block_page = b"<html><body>Attention Required! Cloudflare</body></html>"

    def fake_head(url, *, headers, allow_redirects, timeout):
        raise requests.ConnectionError("HEAD refused for both user agents")

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(
            content=block_page,
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(block_page))},
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="recognised audio container"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_refuses_shortcut_when_head_cannot_verify(tmp_path, monkeypatch):
    """A HEAD failure (server doesn't support it, times out, ...) must never be treated
    as permission to trust the unverified plain-UA download."""
    podcatcher_bytes = b"ID3" + b"the only variant we could actually verify"  # real audio magic

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            raise requests.ConnectionError("HEAD not supported here")
        return _FakeResponse(headers={"Content-Length": str(len(podcatcher_bytes)), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(
            content=podcatcher_bytes,
            headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(podcatcher_bytes))},
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is False
    assert dest.read_bytes() == podcatcher_bytes


def test_fetch_raises_and_leaves_nothing_when_both_head_and_get_length_are_unknown(tmp_path, monkeypatch):
    """Cannot show completeness at all: neither HEAD succeeded (for the chosen UA) nor
    does the GET declare a length. Nothing is written rather than trusting an unverifiable
    stream."""

    def fake_head(url, *, headers, allow_redirects, timeout):
        raise requests.ConnectionError("HEAD unsupported")

    def fake_get(url, *, headers, stream, timeout):
        return _FakeResponse(content=b"whatever this is", headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="cannot show"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_raises_and_leaves_nothing_when_the_connection_fails(tmp_path, monkeypatch):
    def fake_head(url, *, headers, allow_redirects, timeout):
        return _FakeResponse(headers={"Content-Length": "10", "Content-Type": "audio/mpeg"})  # equal both sides

    def fake_get(url, *, headers, stream, timeout):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- byte-sniff (cut-guard's
# second step-2 review: a Content-Type blocklist is a blocklist of what the response
# calls itself, and cannot be completed)


@pytest.mark.parametrize(
    "content_type_header",
    [
        {},  # no Content-Type at all
        {"Content-Type": "text/html, text/html"},  # sent twice; requests joins repeats with ", "
        {"Content-Type": "audio/mpeg"},  # a WAF copying the origin's real type onto a substituted page
    ],
    ids=["absent", "duplicated", "lying-audio-mpeg"],
)
def test_fetch_refuses_html_bytes_no_matter_what_the_content_type_header_claims(tmp_path, monkeypatch, content_type_header):
    """The three header shapes cut-guard verified end to end: none of them are in reach
    of any blocklist (one withholds the signal, one repeats it past a simple membership
    check, one lies using the exact spelling a blocklist would have allowed through).
    The byte-sniff reads the bytes actually written instead, so none of these header
    shapes matter to it -- all three must raise, and none may write a surviving file."""
    html_body = b"<html><body>please enable JavaScript and cookies</body></html>"

    def fake_head(url, *, headers, allow_redirects, timeout):
        raise requests.ConnectionError("HEAD refused for both user agents")

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        get_headers = {"Content-Length": str(len(html_body))}
        get_headers.update(content_type_header)
        return _FakeResponse(content=html_body, headers=get_headers)

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="recognised audio container"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_refuses_html_bytes_labelled_audio_on_the_shortcut_path(tmp_path, monkeypatch):
    """The lying audio/mpeg row also reaches the shortcut path, and there it defeats more
    than a blocklist would have: require_audio_content_type's own "startswith audio/"
    check is satisfied by the label too, since "audio/mpeg" really does start with
    "audio/". Only the byte-sniff, which does not consult the header at all, stops a
    substituted page from being published as the verified master."""
    # Exactly the shape of test_fetch_uses_shortcut_for_a_verified_master's band, but the
    # plain-UA bytes are markup, not audio -- the HEAD's Content-Type is the only thing
    # that lies here, and it lies the same way on the GET.
    html_body = (b"<html><body>rate limited, please retry</body></html>" + b"." * 1000)[:1000]
    podcatcher_bytes = b"x" * 1030  # 2.9% larger: inside the measured band

    def fake_head(url, *, headers, allow_redirects, timeout):
        n = len(html_body) if headers["User-Agent"] == fetch.PLAIN_USER_AGENT else len(podcatcher_bytes)
        return _FakeResponse(headers={"Content-Length": str(n), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PLAIN_USER_AGENT
        return _FakeResponse(
            content=html_body, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(html_body))}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="recognised audio container"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- non-positive length


def test_fetch_refuses_a_zero_length_200_response(tmp_path, monkeypatch):
    """`written != target` is vacuous when target is 0: an empty response would
    otherwise pass by the vacuous 0 == 0 and publish a 0-byte file as a complete episode,
    forever. No HEAD succeeds here, so target comes straight from the GET's own
    (self-consistently empty) Content-Length."""

    def fake_head(url, *, headers, allow_redirects, timeout):
        raise requests.ConnectionError("HEAD refused for both user agents")

    def fake_get(url, *, headers, stream, timeout):
        return _FakeResponse(content=b"", headers={"Content-Type": "audio/mpeg", "Content-Length": "0"})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="target length of 0"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_refuses_when_podcatcher_head_reports_a_zero_content_length(tmp_path, monkeypatch):
    """Common from hosts that don't really implement HEAD: they answer 200 with
    Content-Length: 0 rather than failing outright. shortcut_applies's own
    `podcatcher.length <= 0` guard exists exactly for this -- among other things, it is
    what stops `shrink = (podcatcher.length - plain.length) / podcatcher.length` from
    dividing by zero -- so the plain UA HEAD must succeed here too: a failing plain HEAD
    would make `shortcut_applies` return False on its earlier `plain is None` check
    instead, without ever reaching the guard this test is about.  The fallback download
    that follows then hits _download's own separate target<=0 refusal too, since the
    zero carries forward as expected_length -- both guards matter, and this exercises the
    first one directly."""

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            return _FakeResponse(headers={"Content-Length": "500000", "Content-Type": "audio/mpeg"})
        return _FakeResponse(headers={"Content-Length": "0", "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(content=b"", headers={"Content-Type": "audio/mpeg", "Content-Length": "0"})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="target length of 0"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- partial content


def test_fetch_refuses_a_206_partial_content_response(tmp_path, monkeypatch):
    """raise_for_status() passes any 2xx, and with no usable HEAD a 206 declares
    Content-Length for only the slice it sends -- agreeing with itself and with what
    actually gets written, so every length check above would pass.  This is the one
    hostile response that would otherwise publish a real, playable prefix of the episode
    instead of silence or markup.  requests sends no Range header today, but this must
    not depend on that staying true."""
    partial = b"ID3" + b"only the opening seconds of the episode, then nothing"

    def fake_head(url, *, headers, allow_redirects, timeout):
        raise requests.ConnectionError("HEAD refused for both user agents")

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(
            content=partial,
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": str(len(partial)),
                "Content-Range": f"bytes 0-{len(partial) - 1}/1000000",
            },
            status_code=206,
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="206"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- fallback floor (F1:
# a plain-UA HEAD measurement is real evidence and must not be discarded just because
# the podcatcher HEAD that would normally justify the fallback download is unusable)


def test_fetch_refuses_an_implausibly_small_fallback_when_only_the_plain_head_measured_the_size(tmp_path, monkeypatch):
    """cut-guard's matrix: a plain-UA HEAD that measured the real episode's size is real
    evidence even when the shortcut itself doesn't apply and the podcatcher HEAD can't be
    used at all (here: a 405, common for hosts that don't support HEAD for every UA).
    Discarding that measurement entirely let a self-consistent, audio-shaped decoy far
    smaller than the real episode -- its own Content-Length matching its own tiny body,
    its own bytes starting with real audio magic -- sail past both the byte-count check
    and the sniff and get published as the complete episode."""
    plain_measured = 59_198_300
    decoy = b"ID3" + b"d" * 4_997  # 5000 bytes: begins with real audio magic, self-consistent

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            return _FakeResponse(headers={"Content-Length": str(plain_measured), "Content-Type": "audio/mpeg"})
        raise requests.ConnectionError("405 Method Not Allowed")

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(content=decoy, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(decoy))})

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="implausibly"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


def test_fetch_fallback_is_not_refused_merely_for_being_no_smaller_than_the_plain_head_floor(tmp_path, monkeypatch):
    """Direction check: the floor above must fire only on an implausibly *small* target,
    never merely because the fallback download is about the same size as, or larger
    than, what the plain-UA HEAD measured -- the normal case, since the podcatcher
    variant usually carries advertising on top of the same master and so is a little
    larger, not smaller."""
    plain_measured = 600_000
    real_episode = b"ID3" + b"e" * 599_997  # same size as the plain-UA HEAD measured

    def fake_head(url, *, headers, allow_redirects, timeout):
        if headers["User-Agent"] == fetch.PLAIN_USER_AGENT:
            return _FakeResponse(headers={"Content-Length": str(plain_measured), "Content-Type": "audio/mpeg"})
        raise requests.ConnectionError("405 Method Not Allowed")

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _FakeResponse(
            content=real_episode, headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(real_episode))}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    result = fetch.fetch_episode("http://origin.test/ep.mp3", dest)

    assert result.used_shortcut is False
    assert dest.read_bytes() == real_episode


# ---------------------------------------------------------------- _download cleans up its
# own partial file on every refusal (F6), not only the two checks that happen to run
# after the file is fully written


def test_download_cleans_up_a_partial_file_when_the_connection_drops_mid_stream(tmp_path, monkeypatch):
    """_download must remove whatever it already wrote to dest on *any* refusal, not only
    written != target and the sniff (the two checks that happen to run after the file is
    fully written).  A connection that drops partway through iter_content -- after some
    chunks already landed on disk -- is turned into a FetchError by the except clause
    wrapping the whole GET, and dest must not survive that either: before this fix,
    nothing in _download cleaned up this specific case, and it was left to whichever
    caller remembered to (today, only server.py does)."""
    target_length = 1_000_000

    class _DropsMidStream:
        status_code = 200
        headers = {"Content-Type": "audio/mpeg", "Content-Length": str(target_length)}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"ID3" + b"x" * 9_997  # 10000 bytes actually land on disk
            raise requests.ConnectionError("connection reset mid-stream")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_head(url, *, headers, allow_redirects, timeout):
        # equal sizes both sides: no shortcut, exercise the podcatcher/fallback path
        return _FakeResponse(headers={"Content-Length": str(target_length), "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        assert headers["User-Agent"] == fetch.PODCATCHER_USER_AGENT
        return _DropsMidStream()

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="failed"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- _parse_content_length
# (F5: str.isdigit() accepts Unicode digits int() itself rejects)


def test_parse_content_length_rejects_a_unicode_digit_int_itself_would_reject():
    """str.isdigit() returns True for a superscript two, but int() raises ValueError on
    it -- an uncaught ValueError would show up past server.py's `except fetch.FetchError`
    as an unhandled 500 instead of the deliberate 502 a refusal produces there."""
    assert fetch._parse_content_length("²") is None  # "²" == superscript two


def test_fetch_raises_fetcherror_not_valueerror_when_content_length_is_a_unicode_digit(tmp_path, monkeypatch):
    """End-to-end version of the same gap: a real origin's Content-Length header (HEAD
    and GET both, here) using a digit str.isdigit() accepts but int() rejects must still
    surface as a FetchError like any other unusable measurement, never as a bare,
    uncaught ValueError."""
    weird_length = "²"  # isdigit() is True, int() raises ValueError

    def fake_head(url, *, headers, allow_redirects, timeout):
        return _FakeResponse(headers={"Content-Length": weird_length, "Content-Type": "audio/mpeg"})

    def fake_get(url, *, headers, stream, timeout):
        return _FakeResponse(
            content=b"ID3" + b"x" * 100, headers={"Content-Type": "audio/mpeg", "Content-Length": weird_length}
        )

    monkeypatch.setattr(fetch.requests, "head", fake_head)
    monkeypatch.setattr(fetch.requests, "get", fake_get)

    dest = tmp_path / "episode.mp3"
    with pytest.raises(fetch.FetchError, match="cannot show"):
        fetch.fetch_episode("http://origin.test/ep.mp3", dest)
    assert not dest.exists()


# ---------------------------------------------------------------- _sniff_audio_magic
# (F2: the frame-sync arm accepted the UTF-16LE BOM)


def test_sniff_audio_magic_rejects_the_utf16le_bom_that_used_to_pass_the_sync_check_alone():
    """\\xff\\xfe -- the UTF-16LE byte-order mark -- satisfies the bare 11-bit sync-word
    check (32 of 256 possible second bytes do), which is how an 88-byte UTF-16LE block
    page got published and served as audio/mpeg.  Rejecting the reserved sample-rate
    index this specific page's third byte carries (an ASCII '<' encoded as UTF-16LE)
    closes that gap."""
    bom_page = b"\xff\xfe" + "<html>please enable JavaScript</html>".encode("utf-16-le")
    assert fetch._sniff_audio_magic(bom_page) is False


def test_sniff_audio_magic_still_accepts_a_real_frame_sync():
    """The exact bytes a real fixture begins with (var/fixtures/audio/solved-*.mp3):
    version MPEG1, layer III, a real bitrate index, and sample-rate 44.1kHz -- none of
    them reserved, so the tightened check must still accept it."""
    assert fetch._sniff_audio_magic(b"\xff\xfbr@" + b"\x00" * 8) is True
