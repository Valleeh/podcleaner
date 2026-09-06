"""Offline tests for podcleaner.store: episode keying, the per-episode lock, publish, and
the two JSON sidecars (source binding, fetch provenance).

The keying tests are the point of the first half of this file: cut-guard's step-2 review
found that keying on the guid alone lets two different feeds using the same guid (only
required unique *within* a feed, and a low-entropy numeric guid is common) collide -- one
show's subscriber silently receiving another show's episode.  The lock tests then check
the same coalescing guarantee (docs/architecture.md) still holds once the key is a pair.

The source-binding tests are the point of the second half: what matters is that a (feed,
guid) pair nobody has recorded a source for reads back as unknown rather than as anything
a caller might supply.
"""

from __future__ import annotations

import threading
import time

import pytest

from podcleaner import store

FEED_A = "https://feeds.example/showA.xml"
FEED_B = "https://feeds.example/showB.xml"


def test_episode_path_is_deterministic_and_keyed_by_guid(tmp_path):
    a1 = store.episode_path(FEED_A, "guid-1", root=tmp_path)
    a2 = store.episode_path(FEED_A, "guid-1", root=tmp_path)
    b = store.episode_path(FEED_A, "guid-2", root=tmp_path)

    assert a1 == a2
    assert a1 != b
    assert a1.name == "audio.mp3"
    assert a1.parent.parent == tmp_path


def test_episode_path_differs_for_the_same_guid_in_different_feeds(tmp_path):
    """The bug cut-guard found: a guid is only unique within its own feed. Two shows
    using the same low-entropy guid ("12345") must not resolve to the same file, or one
    show's subscriber gets served a full episode of the other."""
    same_guid = "12345"
    path_a = store.episode_path(FEED_A, same_guid, root=tmp_path)
    path_b = store.episode_path(FEED_B, same_guid, root=tmp_path)
    assert path_a != path_b


def test_episode_path_honours_a_monkeypatched_store_root(tmp_path, monkeypatch):
    """STORE_ROOT is read fresh inside the function, not bound as a default argument at
    import time -- otherwise this (and any test using root= implicitly) could not
    redirect storage after the module is already loaded."""
    monkeypatch.setattr(store, "STORE_ROOT", tmp_path)
    assert store.episode_path(FEED_A, "guid-x").parent.parent == tmp_path


@pytest.mark.parametrize("bad_feed_url", ["", "   "])
def test_episode_key_refuses_empty_feed_url(bad_feed_url):
    with pytest.raises(store.StoreError):
        store.episode_key(bad_feed_url, "guid-1")


@pytest.mark.parametrize("bad_guid", ["", "   "])
def test_episode_key_refuses_empty_guid(bad_guid):
    with pytest.raises(store.StoreError):
        store.episode_key(FEED_A, bad_guid)


def test_episode_key_does_not_collide_when_a_newline_could_shift_the_boundary():
    """cut-guard's second step-2 review: hashing a single "\\n"-joined string let
    feed_url="http://x.test/a", guid="b\\nc" and feed_url="http://x.test/a\\nb", guid="c"
    concatenate to the identical string ("http://x.test/a\\nb\\nc" either way) and
    therefore hash to the same key. Hashing feed_url and guid separately before
    combining the two fixed-length digests makes that impossible by construction."""
    key1 = store.episode_key("http://x.test/a", "b\nc")
    key2 = store.episode_key("http://x.test/a\nb", "c")
    assert key1 != key2


def test_episode_lock_serialises_the_same_feed_and_guid():
    guard = threading.Lock()
    state = {"inside": 0, "max_inside": 0}

    def worker():
        with store.episode_lock(FEED_A, "same-guid"):
            with guard:
                state["inside"] += 1
                state["max_inside"] = max(state["max_inside"], state["inside"])
            time.sleep(0.05)
            with guard:
                state["inside"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max_inside"] == 1


def test_episode_lock_does_not_block_the_same_guid_in_a_different_feed():
    """Also covers the (feed, guid) pairing: two different feeds sharing a guid must
    coalesce independently, not accidentally serialise on the guid alone."""
    order = []
    holder_started = threading.Event()

    def holder():
        with store.episode_lock(FEED_A, "shared-guid"):
            holder_started.set()
            time.sleep(0.2)
            order.append("a")

    def other():
        holder_started.wait(timeout=2)
        with store.episode_lock(FEED_B, "shared-guid"):
            order.append("b")

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=other)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # "b" never waits on "a"'s lock, so it finishes first despite starting second.
    assert order == ["b", "a"]


def test_publish_moves_file_atomically_and_creates_parent_dirs(tmp_path):
    tmp = tmp_path / "work" / "audio.mp3.part"
    tmp.parent.mkdir(parents=True)
    tmp.write_bytes(b"finished episode bytes")
    final = tmp_path / "episodes" / "abc" / "audio.mp3"

    result = store.publish(tmp, final)

    assert result == final
    assert final.read_bytes() == b"finished episode bytes"
    assert not tmp.exists()


# ---------------------------------------------------------------- source binding


def test_unrecorded_pair_has_no_source(tmp_path):
    """The security-relevant default: a (feed, guid) pair nobody has recorded a source
    for -- in particular, one only ever supplied by a caller of /podcast -- reads back
    as unknown."""
    assert store.read_source(FEED_A, "nobody-has-named-this-guid", root=tmp_path) is None


def test_read_source_returns_what_was_recorded(tmp_path):
    store.record_source(FEED_A, "guid-1", "https://origin.test/real-episode.mp3", root=tmp_path)
    assert store.read_source(FEED_A, "guid-1", root=tmp_path) == "https://origin.test/real-episode.mp3"


def test_source_is_keyed_per_feed_not_just_per_guid(tmp_path):
    """The exact scenario cut-guard described: feed A and feed B both use guid 12345.
    Recording B's binding must not disturb A's, and reading A back must still give A's
    URL, not B's."""
    store.record_source(FEED_A, "12345", "https://origin.test/showA-real-episode.mp3", root=tmp_path)
    store.record_source(FEED_B, "12345", "https://origin.test/showB-real-episode.mp3", root=tmp_path)

    assert store.read_source(FEED_A, "12345", root=tmp_path) == "https://origin.test/showA-real-episode.mp3"
    assert store.read_source(FEED_B, "12345", root=tmp_path) == "https://origin.test/showB-real-episode.mp3"


def test_record_source_is_overwritable_per_feed(tmp_path):
    store.record_source(FEED_A, "guid-1", "https://origin.test/a.mp3", root=tmp_path)
    # idempotent overwrite: the feed's next refresh simply replaces the prior binding
    store.record_source(FEED_A, "guid-1", "https://origin.test/a-moved.mp3", root=tmp_path)
    assert store.read_source(FEED_A, "guid-1", root=tmp_path) == "https://origin.test/a-moved.mp3"


def test_record_source_refuses_an_empty_url(tmp_path):
    with pytest.raises(store.StoreError):
        store.record_source(FEED_A, "guid-1", "", root=tmp_path)


# ---------------------------------------------------------------- fetch provenance


def test_unfetched_episode_has_no_meta(tmp_path):
    assert store.read_meta(FEED_A, "never-fetched-guid", root=tmp_path) is None


def test_write_meta_round_trips_next_to_the_audio(tmp_path):
    store.write_meta(FEED_A, "guid-1", {"used_shortcut": True, "user_agent": "curl/7.88.1"}, root=tmp_path)
    data = store.read_meta(FEED_A, "guid-1", root=tmp_path)
    assert data == {"used_shortcut": True, "user_agent": "curl/7.88.1"}
    assert store.meta_path(FEED_A, "guid-1", root=tmp_path).parent == store.episode_dir(FEED_A, "guid-1", root=tmp_path)


def test_meta_is_also_keyed_per_feed_not_just_per_guid(tmp_path):
    store.write_meta(FEED_A, "12345", {"used_shortcut": True, "user_agent": "curl/7.88.1"}, root=tmp_path)
    assert store.read_meta(FEED_B, "12345", root=tmp_path) is None
