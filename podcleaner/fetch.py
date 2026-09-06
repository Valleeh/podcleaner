"""Download an episode's audio.

Some publishers serve different bytes for the same enclosure URL depending on
``User-Agent``: a plain tool UA gets the ad-free master, every other UA -- a real
podcatcher, or this project's own name -- gets the version with advertising stitched
in.  Measured 2026-09-06 against Lage der Nation and Solved (``var/fixtures/dai/*.json``,
byte-identical splices recovered by :mod:`podcleaner.eval.dai`): ``curl/7.88.1`` is the
UA that returned the master, ``AntennaPod/3.5.0`` is the UA that returned what a
listener's podcatcher actually receives.  Both strings come from that measurement,
neither is a guess, and the publisher can withdraw the shortcut any day.

Before trusting a plain-UA download this module checks, with a HEAD request under each
UA, that the plain fetch is genuinely and *plausibly* smaller than what the podcatcher
UA would get: within a band defended by the measured evidence (see
:data:`_MIN_SHRINK_RATIO` / :data:`_MAX_SHRINK_RATIO`), and reporting a ``Content-Type``
that actually claims to be audio.  A HEAD that fails, disagrees, or falls outside that
band all mean the shortcut is not in effect here, and the podcatcher variant -- the only
thing verified to be the real episode -- is what gets kept.

Even then, a plain-UA HEAD that *did* succeed is not thrown away just because the
podcatcher HEAD that would normally justify the fallback download did not (a 405, or one
with no usable length, are the same condition for :func:`_head_probe`).  With only the
plain measurement known, it still stands as a lower bound on how small the podcatcher
variant can plausibly be, using :data:`_MAX_SHRINK_RATIO` -- already this module's
statement of how much smaller one verified-genuine variant may be than the other -- as
the tolerance.  It is a floor, never a ceiling: the podcatcher variant is normally a
little *larger* than the plain one (it carries the advertising the plain one skips), so
this only refuses a fallback body implausibly smaller than what the plain UA measured,
never one that is merely larger or equal to it.

The actual GET's ``Content-Type`` is checked again on the shortcut path only: "not
positively audio" is weak evidence -- some publishers serve their mp3s as
``application/octet-stream`` -- so ``require_audio_content_type`` applies only where the
plain-UA response is being trusted as a substitute for the real episode and needs its
own justification; the fallback makes no claim beyond "this is what the podcatcher would
get" and must not fail just for being honestly labelled something else.

Whether the response *is* the episode at all used to be decided by a second,
unconditional check: a blocklist of Content-Type spellings that "positively declare"
markup or text.  cut-guard's second step-2 review closed that approach instead of
extending it: a blocklist of what a response chooses to call itself cannot be completed.
A response with no ``Content-Type`` header, one sent twice (``requests`` joins repeated
headers with ``", "``, so two ``text/html`` headers defeat a blocklist containing
``text/html``), or a WAF copying the origin's real ``audio/mpeg`` label onto a
substituted block page all withhold or falsify the one signal a blocklist reads, and all
three published garbage under it.  So this module no longer reads that header for this
question.  Once a download is shown complete (below), :func:`_download` sniffs the bytes
it actually wrote against a small allowlist of container magic (see
:func:`_sniff_audio_magic`) covering what podcasts actually ship -- unconditionally, on
*both* paths.  That is positive evidence about the bytes themselves rather than the
response's claim about them: a block page can be entirely self-consistent in its own
``Content-Length`` (matching its own body, which is why the byte-count check below
cannot substitute for this one), but it does not coincidentally begin with an MP3 frame
sync or an ``ftyp`` box.  It is an allowlist, so a legitimate but unlisted format is
refused too -- deliberately: a 502 here is loud and the podcatcher retries, while
publishing garbage is silent and, once ``episode_path`` exists, permanent.

Every download, on either path, is required to prove its own completeness before it is
returned at all: the byte count actually written must match either the GET response's
own ``Content-Length`` or the length an earlier HEAD measured for that exact URL and UA
(cut-guard's step-2 review: a 2xx with neither a declared length nor chunked framing --
which an on-the-fly ad-stitcher can emit -- otherwise lets a stream end early with no
exception raised at all, and the truncated file would be published and never re-fetched).
A response whose own numbers disagree with the number that justified fetching it, or that
cannot be shown complete at all, is refused rather than published.  Neither number is
trusted unless it is positive: a zero-length HEAD or GET would otherwise pass the same
check by the vacuous ``0 == 0`` and publish an empty file forever (cut-guard's second
step-2 review; some origins answer ``HEAD`` with ``Content-Length: 0`` rather than truly
supporting it).  And the status code has to be a plain ``200``: ``raise_for_status()``
passes any 2xx, so nothing else here would notice a ``206 Partial Content`` that declares
-- accurately -- the ``Content-Length`` of only the slice it sends, which is complete by
every check above at that smaller size.  This proves the download complete, never that
it is the episode: a block page reporting its own accurate ``Content-Length`` is just as
"complete" at that size, which is why none of this can substitute for the byte-sniff
above.

No detection or cutting happens in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import requests

from podcleaner.logging import get_logger

__all__ = [
    "FetchError",
    "FetchResult",
    "HeadInfo",
    "PLAIN_USER_AGENT",
    "PODCATCHER_USER_AGENT",
    "fetch_episode",
    "shortcut_applies",
]

logger = get_logger(__name__)

PathLike = Union[str, Path]

#: Measured 2026-09-06 (see var/fixtures/dai/*.json): this UA is the one that actually
#: returned the ad-free master for Lage der Nation and Solved.
PLAIN_USER_AGENT = os.environ.get("PODCLEANER_PLAIN_UA", "curl/7.88.1")

#: The UA a real podcatcher sends; every listener gets this variant, ads included.  Also
#: used as the honest fallback download when the shortcut cannot be verified.
PODCATCHER_USER_AGENT = os.environ.get("PODCLEANER_PODCATCHER_UA", "AntennaPod/3.5.0")

_TIMEOUT = 120
_CHUNK = 1 << 16  # 64 KiB: stream to disk, never hold a whole episode in memory

# Measured 2026-09-06 from the pinned fixture pairs (var/fixtures/audio/, exact byte
# counts): a verified ad-free master is smaller than the podcatcher variant by
#   LdN490  (57977837 / 59198300) -> 2.06%
#   LdN491  (66296547 / 68484653) -> 3.19%
#   Solved (180247406 / 182340754) -> 1.15%
# The band below keeps roughly 2.5x the highest measured ratio as headroom for
# publishers not in the measured set, while staying far short of where a WAF/bot-manager
# challenge page, an empty response, a trailer clip or a rights-shortened cut would land
# -- cut-guard's step-2 review put those at 90%+ smaller, not a few percent.  Reachable
# HEAD-only attacks the content-type check alone would not catch (e.g. a same-size
# decoy) are why both checks exist.  Widen only against new measured evidence.
_MIN_SHRINK_RATIO = 0.005  # 0.5%: reject "same file, noise" as proof of the shortcut
_MAX_SHRINK_RATIO = 0.08  # 8%: about 2.5x the highest measured genuine ratio (3.19%)


class FetchError(RuntimeError):
    """Raised when the episode could not be downloaded, or could not be shown complete."""


@dataclass(frozen=True)
class FetchResult:
    path: Path
    used_shortcut: bool  # True: the plain-UA master was verified and kept
    user_agent: str
    bytes_written: int


@dataclass(frozen=True)
class HeadInfo:
    length: int
    content_type: str


def shortcut_applies(plain: Optional[HeadInfo], podcatcher: Optional[HeadInfo]) -> bool:
    """Whether the plain-UA fetch is verified to have skipped inserted advertising.

    Requires both HEAD probes to have succeeded, the plain response to actually claim to
    be audio (rejects an HTML block/challenge page outright regardless of its size -- the
    same User-Agent discrimination the shortcut relies on is exactly what a WAF is also
    likely to key on), and the size reduction to fall within the measured band.  Equal,
    larger, too small a reduction to be more than noise, or too large to be a plausible
    ad load -- all of that means the shortcut cannot be trusted here.
    """
    if plain is None or podcatcher is None:
        return False
    if not plain.content_type.lower().startswith("audio/"):
        return False
    if podcatcher.length <= 0:
        return False
    shrink = (podcatcher.length - plain.length) / podcatcher.length
    return _MIN_SHRINK_RATIO <= shrink <= _MAX_SHRINK_RATIO


def _parse_content_length(value: Optional[str]) -> Optional[int]:
    """An ``int()``-parseable length, or ``None`` if the header is absent or not one.

    ``str.isdigit()`` is not a safe pre-check: it accepts Unicode digits (a superscript
    two, for instance) that ``int()`` itself then raises ``ValueError`` on.  That
    ``ValueError`` would otherwise escape both call sites uncaught -- past every
    ``FetchError`` handler downstream, including ``server.py``'s -- as an unhandled 500
    instead of the deliberate 502 a refusal produces there.  Parsing directly and
    treating a rejection the same as an absent header closes that without changing what
    an ordinary integer header does.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _content_type_base(content_type: str) -> str:
    """``content_type`` without a ``; charset=...`` parameter, lower-cased."""
    return content_type.split(";", 1)[0].strip().lower()


_SNIFF_BYTES = 12  # covers every magic sequence below, including RIFF/WAVE at offset 8-12


def _sniff_audio_magic(head: bytes) -> bool:
    """Whether ``head`` -- the first bytes of a file this module downloaded -- begins
    with the magic of a container podcasts actually ship: an ID3v2 tag, a raw MPEG frame
    sync with no ID3 tag ahead of it, an ISO base media (m4a/mp4) ``ftyp`` box, Ogg,
    FLAC, or RIFF/WAVE.  Deliberately an allowlist, not a "not text" heuristic -- see the
    module docstring for why a blocklist of what a response calls itself cannot be
    completed, and for the trade an allowlist makes instead.

    The frame-sync arm checks more than the 11-bit sync word: that alone is only 3 fixed
    bits in the second byte, leaving 5 free (32 of 256 possible second bytes satisfy it),
    and ``\\xff\\xfe`` -- the UTF-16LE byte-order mark -- is one of them.  An 88-byte
    UTF-16LE block page was published as ``audio/mpeg`` under the sync check alone.  A
    real encoder never sets version to the reserved ``01``, layer to the reserved ``00``,
    the bitrate index to the reserved "bad" ``1111``, or the sample-rate index to the
    reserved ``11``; rejecting a frame that claims any of those closes that gap without
    touching the real frame syncs this exists to accept.
    """
    if head.startswith(b"ID3"):
        return True
    if len(head) >= 3 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        version = (head[1] >> 3) & 0b11
        layer = (head[1] >> 1) & 0b11
        bitrate = (head[2] >> 4) & 0b1111
        sample_rate = (head[2] >> 2) & 0b11
        if version != 0b01 and layer != 0b00 and bitrate != 0b1111 and sample_rate != 0b11:
            return True
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    if head.startswith((b"OggS", b"fLaC")):
        return True
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    return False


def _head_probe(url: str, user_agent: str) -> Optional[HeadInfo]:
    """HEAD ``url`` under ``user_agent``; ``None`` if its length can't be trusted at all."""
    try:
        resp = requests.head(url, headers={"User-Agent": user_agent}, allow_redirects=True, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("fetch_head_failed", url=url, user_agent=user_agent, error=str(exc))
        return None
    length = _parse_content_length(resp.headers.get("Content-Length"))
    if length is None:
        logger.warning("fetch_head_no_length", url=url, user_agent=user_agent)
        return None
    return HeadInfo(length=length, content_type=resp.headers.get("Content-Type", ""))


def _download(
    url: str,
    user_agent: str,
    dest: Path,
    *,
    expected_length: Optional[int],
    require_audio_content_type: bool,
    plain_head_length: Optional[int] = None,
) -> int:
    """Stream ``url`` to ``dest`` under ``user_agent``; return the bytes actually written.

    Checks run in this order and, except where noted, apply on *both* the shortcut and
    the fallback path -- see the module docstring for why each one exists:

    * a plain ``200`` (a 206 would declare, and satisfy, a length for only the slice it
      sends);
    * ``require_audio_content_type``, **shortcut path only**: "not positively audio" is
      not evidence of anything on the fallback path -- some publishers serve their mp3s
      as ``application/octet-stream`` (an unconfigured S3 bucket is a common cause) --
      and enforcing it there would turn an honest, ad-laden download into a hard failure
      with no episode at all.  This decides whether to *trust the shortcut*; it is not
      the check that decides whether the bytes are an episode;
    * ``expected_length`` agreement, when an earlier HEAD under this same UA measured
      one -- the length that justified fetching this URL this way in the first place, so
      a GET declaring a different one is refused outright (a HEAD and a GET are two
      different requests a CDN could answer differently), and with no length on either
      side, nothing is written at all;
    * the resolved target is positive (rejects a zero-length HEAD or GET, which would
      otherwise pass ``written == target`` at 0);
    * ``plain_head_length``, **fallback path only**, when the caller passes one: a lower
      bound on the resolved target using :data:`_MAX_SHRINK_RATIO` as the tolerance --
      see the module docstring for why this exists and why it is a floor, never a
      ceiling;
    * the byte count written matches the target exactly, catching a connection that
      closes early with no declared length and no chunked framing to violate;
    * once the file is written, :func:`_sniff_audio_magic` against the bytes actually on
      disk -- on both paths, this is what decides whether the bytes are an episode at
      all, replacing a ``Content-Type`` blocklist that could not be completed.

    Any failure here is a refusal, never a partial publish: a 502 is loud and the
    podcatcher retries, which is strictly better than a truncated, empty, or fake file
    cached and served forever.  Refusing always leaves ``dest`` exactly as it was before
    this call: any file this function itself wrote is removed before a
    :class:`FetchError` leaves it, so a caller never has to remember to do that itself.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            with requests.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=_TIMEOUT) as resp:
                resp.raise_for_status()
                if resp.status_code != 200:
                    raise FetchError(
                        f"GET {url} ({user_agent!r}) returned status {resp.status_code}, not a plain "
                        f"200; refusing to publish a partial or non-standard response as a complete episode"
                    )
                content_type = resp.headers.get("Content-Type", "")
                base_content_type = _content_type_base(content_type)
                if require_audio_content_type and not base_content_type.startswith("audio/"):
                    raise FetchError(
                        f"GET {url} ({user_agent!r}) returned Content-Type {content_type!r}, not audio; "
                        f"refusing to publish it as the verified master"
                    )
                get_length = _parse_content_length(resp.headers.get("Content-Length"))
                if expected_length is not None and get_length is not None and get_length != expected_length:
                    raise FetchError(
                        f"GET {url} ({user_agent!r}) declared Content-Length {get_length}, but the "
                        f"earlier HEAD that justified this fetch measured {expected_length}; refusing"
                    )
                target = expected_length if expected_length is not None else get_length
                if target is None:
                    raise FetchError(
                        f"GET {url} ({user_agent!r}) declared no Content-Length and no earlier HEAD "
                        f"measured one either; cannot show the download would be complete"
                    )
                if target <= 0:
                    raise FetchError(
                        f"GET {url} ({user_agent!r}) resolved a target length of {target}; refusing to "
                        f"treat an empty or negative-length response as a complete episode"
                    )
                if plain_head_length is not None:
                    floor = plain_head_length * (1 - _MAX_SHRINK_RATIO)
                    if target < floor:
                        raise FetchError(
                            f"GET {url} ({user_agent!r}) resolved a target length of {target}, implausibly "
                            f"smaller than the {plain_head_length} a plain-UA HEAD measured for this same "
                            f"URL -- the podcatcher variant is never more than {_MAX_SHRINK_RATIO:.0%} "
                            f"smaller than that measurement; refusing to treat a body this much smaller as "
                            f"the complete episode"
                        )
                written = 0
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        fh.write(chunk)
                        written += len(chunk)
        except requests.RequestException as exc:
            raise FetchError(f"GET {url} ({user_agent!r}) failed: {exc}") from exc

        if written != target:
            raise FetchError(
                f"GET {url} ({user_agent!r}) wrote {written} bytes, expected {target}; refusing to "
                f"publish a file that cannot be shown complete"
            )

        with dest.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES)
        if not _sniff_audio_magic(head):
            raise FetchError(
                f"GET {url} ({user_agent!r}) wrote {written} bytes to {dest} that do not begin with a "
                f"recognised audio container; first bytes were {head!r}; refusing to publish it as the "
                f"episode"
            )
        return written
    except FetchError:
        dest.unlink(missing_ok=True)
        raise


def fetch_episode(url: str, dest: PathLike) -> FetchResult:
    """Download the episode at ``url`` to ``dest``, verifying the shortcut before trusting it.

    ``dest`` is written directly; this function does not itself publish atomically --
    that is :mod:`podcleaner.store`'s job, so callers pass a working path and rename it
    into place once this returns.
    """
    dest = Path(dest)
    plain = _head_probe(url, PLAIN_USER_AGENT)
    podcatcher = _head_probe(url, PODCATCHER_USER_AGENT)

    if shortcut_applies(plain, podcatcher):
        logger.info("fetch_shortcut_used", url=url, plain_bytes=plain.length, podcatcher_bytes=podcatcher.length)
        written = _download(
            url, PLAIN_USER_AGENT, dest, expected_length=plain.length, require_audio_content_type=True
        )
        return FetchResult(dest, True, PLAIN_USER_AGENT, written)

    logger.info(
        "fetch_shortcut_absent",
        url=url,
        plain_bytes=plain.length if plain else None,
        podcatcher_bytes=podcatcher.length if podcatcher else None,
    )
    fallback_expected = podcatcher.length if podcatcher is not None else None
    # podcatcher's own HEAD is exactly what's missing here (a usable one would have made
    # fallback_expected non-None above).  When plain's succeeded anyway, its measurement
    # is real evidence about this same URL, so it is used as a floor inside _download
    # rather than discarded outright -- see the module docstring and _download's
    # plain_head_length parameter.
    plain_head_length = plain.length if (podcatcher is None and plain is not None) else None
    written = _download(
        url,
        PODCATCHER_USER_AGENT,
        dest,
        expected_length=fallback_expected,
        require_audio_content_type=False,
        plain_head_length=plain_head_length,
    )
    return FetchResult(dest, False, PODCATCHER_USER_AGENT, written)
