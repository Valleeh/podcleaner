"""Where a fetched episode lives on disk, and how concurrent requests for it coordinate.

The filesystem is the state: the audio file existing at its path means the episode is
ready to serve; not existing means it must be produced.  No database, no in-memory
registry of episodes -- docs/mvp.md deliberately drops the queue/worker/lease machinery
the full design describes, because the MVP is one process serving one listener, and a
second process asking for the same episode would see exactly what this one already
wrote to disk.

The episode key is the feed URL *and* the guid together, not the guid alone.  A guid
is only required to be unique within its own feed (RSS spec), and `docs/mvp.md`'s own
label table spans four different shows through one server -- not a hypothetical, the
expected usage.  Two different feeds using the same guid (`<guid isPermaLink="false">
12345</guid>` is a common, low-entropy choice) would otherwise collide: whichever feed
was fetched most recently would silently overwrite the other's binding, and a listener
of one show would be served an episode of a different one entire.  It is not
`md5(mp3_url)` either, per the same file's existing note: CDNs vary the enclosure URL
per request, so keying on it would miss the cache on almost every request.  The feed
URL does not vary that way, so it is safe to key on directly.

Two small JSON sidecars live next to the audio, each written wholesale (never merged, so
there is no read-modify-write race):

``source.json``
    The origin enclosure URL a feed *this server itself fetched* named for this guid,
    recorded by ``/rss`` before any episode is ever requested.  ``/podcast`` looks the
    URL up from here instead of trusting a caller-supplied one.  What this does and does
    not prevent: a caller cannot make a request that lands arbitrary bytes at an episode
    identity a *different, real* feed already named -- that would require this server to
    fetch that real feed's own URL and have it return attacker content, which requires
    controlling that origin, not just calling this server.  It does **not** prevent an
    unauthenticated caller from pointing ``/rss?feed=`` at a feed they host themselves and
    having this server fetch and cache whatever it says, under the (feed, guid) identity
    that document itself declares -- there is no feed allowlist.  That is the same shape
    as the already-accepted "no authentication" risk (`docs/mvp.md`, After the MVP): free
    unauthenticated work, not corruption of a real episode's identity.

``meta.json``
    Fetch provenance -- whether the shortcut fired -- written after a successful fetch,
    before the audio is published.  The filesystem is the state, so whether an episode's
    audio is the verified master or the full pipeline's fallback has to live here too:
    docs/mvp.md step 4 only runs detection "when step 2's fetch found no clean master",
    and that decision cannot be recovered from the audio bytes alone after the fact.
    ``read_meta`` returning ``None`` means provenance is unknown and must be treated as
    "detection still has to run" -- never as "already verified clean".  Defaulting a
    missing ``used_shortcut`` to ``True`` would skip detection on an ad-laden file; that
    is the same shape of mistake the ``degraded`` flag's static default was in step 1.

Neither sidecar is invalidated automatically once the audio exists.  If a publisher
re-uploads an episode under the *same* guid with a different URL (a takedown, a
re-edit), ``source.json`` picks up the new URL on the next ``/rss`` fetch, but
``episode_path`` already exists, so ``/podcast``'s fast path never re-fetches, and the
withdrawn file is served forever.  This cannot be told apart from ordinary per-request
CDN URL variance by comparing URLs -- that variance is exactly why the cache is keyed on
(feed, guid) and not on the URL in the first place, so a changed URL is not evidence of
a changed episode.  Recovery is manual: delete the episode's directory
(``rm -rf var/episodes/<key>/``) and the next request re-fetches from the current
source.  Deliberately not automated; see `docs/mvp.md`'s After the MVP list.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

__all__ = [
    "StoreError",
    "episode_dir",
    "episode_key",
    "episode_lock",
    "episode_path",
    "meta_path",
    "publish",
    "read_meta",
    "read_source",
    "record_source",
    "source_path",
    "write_meta",
]

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Read once here; functions below re-read this name (not a bound default argument) so
#: tests can redirect it with monkeypatch after import.
STORE_ROOT = Path(os.environ.get("PODCLEANER_STORE_ROOT", str(REPO_ROOT / "var" / "episodes")))


class StoreError(RuntimeError):
    """Raised when an episode has no usable feed URL and guid to key its storage on."""


def episode_key(feed_url: str, guid: str) -> str:
    """A filesystem-safe, stable identifier for the episode with this guid *in this feed*."""
    if not feed_url or not feed_url.strip():
        raise StoreError("cannot store an episode without its feed url")
    if not guid or not guid.strip():
        raise StoreError("cannot store an episode without a guid")
    # feed_url and guid are hashed separately, and the two fixed-length (32-byte)
    # digests are concatenated before the final hash -- so the boundary between them
    # sits at a fixed offset no content in either part can move, unlike a single hash
    # over a delimited join of the two.
    feed_digest = hashlib.sha256(feed_url.strip().encode("utf-8")).digest()
    guid_digest = hashlib.sha256(guid.strip().encode("utf-8")).digest()
    return hashlib.sha256(feed_digest + guid_digest).hexdigest()[:32]


def episode_dir(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Path:
    root = STORE_ROOT if root is None else Path(root)
    return root / episode_key(feed_url, guid)


def episode_path(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Path:
    """Where the ad-free audio for this episode lives once it is ready."""
    return episode_dir(feed_url, guid, root=root) / "audio.mp3"


def source_path(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Path:
    return episode_dir(feed_url, guid, root=root) / "source.json"


def meta_path(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Path:
    return episode_dir(feed_url, guid, root=root) / "meta.json"


# One lock per episode key, created on first use and kept for the life of the process.
# Never evicted: the MVP is one process serving one listener, so the number of distinct
# episodes touched in a run is small -- a real cache-eviction policy would be solving a
# problem that has not happened (docs/mvp.md).
_registry_guard = threading.Lock()
_locks: Dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _registry_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def episode_lock(feed_url: str, guid: str) -> Iterator[None]:
    """Serialise requests for one episode.  A request for a different episode -- a
    different guid, or the same guid in a different feed -- never waits on this one."""
    lock = _lock_for(episode_key(feed_url, guid))
    with lock:
        yield


def publish(tmp_path: Path, final_path: Path) -> Path:
    """Move a finished file into place atomically: a reader sees nothing, or the whole
    file, never a partial one."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, final_path)
    return final_path


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def record_source(feed_url: str, guid: str, url: str, *, root: Optional[Path] = None) -> Path:
    """Record the enclosure URL a feed this server fetched actually named for this guid.

    ``/podcast`` only ever fetches a URL read back from here -- never one a caller
    supplies.  Idempotent: a later call for the same (feed, guid) pair -- the feed's
    next refresh -- simply overwrites this with whatever that feed names now.  See this
    module's docstring for exactly what that does and does not defend against.
    """
    if not url or not url.strip():
        raise StoreError(f"cannot record an empty source url for guid {guid!r}")
    return _write_json_atomic(source_path(feed_url, guid, root=root), {"url": url, "feed": feed_url})


def read_source(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Optional[str]:
    """The enclosure URL recorded for this (feed, guid) pair, or ``None`` if no feed
    this server fetched has ever named it."""
    data = _read_json(source_path(feed_url, guid, root=root))
    return data.get("url") if data else None


def write_meta(feed_url: str, guid: str, data: Dict[str, Any], *, root: Optional[Path] = None) -> Path:
    """Record fetch provenance (e.g. ``used_shortcut``) next to the audio."""
    return _write_json_atomic(meta_path(feed_url, guid, root=root), data)


def read_meta(feed_url: str, guid: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch provenance for this episode, or ``None`` if it has never been fetched.

    ``None`` must be read as "provenance unknown, so detection still has to run" -- never
    as "already known clean".  See this module's docstring.
    """
    return _read_json(meta_path(feed_url, guid, root=root))
