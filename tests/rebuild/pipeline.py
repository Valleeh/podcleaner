"""A small but *real* four-stage pipeline used by the durability tests.

It is deliberately not a mock. Each handler reads the artifact the previous
stage wrote and writes its own to a deterministic path, so:

* a handler that never ran leaves a missing file, and
* the next handler then fails loudly instead of quietly producing garbage.

That property is what turns "the row says published" into "the episode really
is published", which is what makes the crash test able to catch the
commit-before-work mutation.

Everything is pure-local: no network, no ffmpeg, no LLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable, Mapping

from podcleaner.core.queue import Lease
from podcleaner.core.states import State

__all__ = [
    "STAGE_SUFFIX",
    "Pipeline",
    "expected_published",
    "raw_audio",
]


#: state a worker claims in -> artifact that stage produces
STAGE_SUFFIX: Mapping[str, str] = {
    State.DISCOVERED.value: "fetched",
    State.FETCHED.value: "analyzed",
    State.ANALYZED.value: "cut",
    State.CUT.value: "published",
}


def raw_audio(guid: str) -> bytes:
    """Deterministic stand-in for downloaded audio bytes."""
    seed = hashlib.sha256(guid.encode()).digest()
    return (seed * 64)[:2048]


def _ad_ranges(raw: bytes) -> list[list[int]]:
    """Deterministic pseudo ad detection: two windows keyed off the content."""
    h = hashlib.sha256(raw).digest()
    a = h[0] * 4
    b = a + 128 + h[1]
    c = b + 256 + h[2] * 2
    d = c + 64 + h[3]
    return [[a, min(b, len(raw))], [min(c, len(raw)), min(d, len(raw))]]


def _apply_cuts(raw: bytes, ranges: list[list[int]]) -> bytes:
    keep = bytearray()
    cursor = 0
    for start, end in ranges:
        keep += raw[cursor:start]
        cursor = max(cursor, end)
    keep += raw[cursor:]
    return bytes(keep)


def expected_published(guid: str) -> dict:
    """Recompute, independently of the workers, what ``<guid>.published`` must hold.

    The test asserts against this instead of against whatever the pipeline
    happened to write, so a skipped stage cannot be papered over.
    """
    raw = raw_audio(guid)
    ranges = _ad_ranges(raw)
    cut = _apply_cuts(raw, ranges)
    return {
        "guid": guid,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "cut_sha256": hashlib.sha256(cut).hexdigest(),
        "cut_bytes": len(cut),
        "removed_bytes": len(raw) - len(cut),
    }


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Pipeline:
    """Handlers for the four working states.

    ``crash_at`` names a state whose handler announces itself (by creating
    ``marker_dir/<state>.marker``) and then blocks forever, so the parent test
    can ``SIGKILL`` the process at a precisely known point: after the claim,
    after the work has *started*, and before any artifact or commit exists.
    """

    def __init__(
        self,
        work_dir: "str | os.PathLike[str]",
        *,
        crash_at: str | None = None,
        marker_dir: "str | os.PathLike[str] | None" = None,
        work_ms: float = 0.0,
    ):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.crash_at = crash_at
        self.marker_dir = Path(marker_dir) if marker_dir else self.work_dir
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        self.work_ms = work_ms

    # ------------------------------------------------------------------ paths

    def artifact(self, guid: str, kind: str) -> Path:
        safe = guid.replace("/", "_")
        return self.work_dir / f"{safe}.{kind}"

    # ------------------------------------------------------------- the stages

    def _begin(self, lease: Lease) -> None:
        """Common preamble: maybe announce-and-block so the parent can kill us."""
        if self.crash_at is not None and lease.state.value == self.crash_at:
            marker = self.marker_dir / f"{self.crash_at}.marker"
            _atomic_write(marker, f"{os.getpid()} {lease.guid}\n".encode())
            # Block until SIGKILLed. No artifact has been written yet and the
            # queue has not been committed, which is exactly the crash the
            # design claims to survive.
            while True:
                time.sleep(0.05)
        if self.work_ms:
            time.sleep(self.work_ms / 1000.0)

    def fetch(self, lease: Lease) -> None:
        self._begin(lease)
        _atomic_write(self.artifact(lease.guid, "fetched"), raw_audio(lease.guid))

    def analyze(self, lease: Lease) -> None:
        self._begin(lease)
        raw = self.artifact(lease.guid, "fetched").read_bytes()  # missing => hard fail
        doc = {
            "guid": lease.guid,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "ad_ranges": _ad_ranges(raw),
        }
        _atomic_write(
            self.artifact(lease.guid, "analyzed"),
            json.dumps(doc, sort_keys=True).encode(),
        )

    def cut(self, lease: Lease) -> None:
        self._begin(lease)
        raw = self.artifact(lease.guid, "fetched").read_bytes()
        doc = json.loads(self.artifact(lease.guid, "analyzed").read_bytes())
        if doc["raw_sha256"] != hashlib.sha256(raw).hexdigest():
            raise ValueError(f"{lease.guid}: analysis does not match fetched audio")
        _atomic_write(
            self.artifact(lease.guid, "cut"), _apply_cuts(raw, doc["ad_ranges"])
        )

    def publish(self, lease: Lease) -> None:
        self._begin(lease)
        raw = self.artifact(lease.guid, "fetched").read_bytes()
        cut = self.artifact(lease.guid, "cut").read_bytes()
        doc = {
            "guid": lease.guid,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "cut_sha256": hashlib.sha256(cut).hexdigest(),
            "cut_bytes": len(cut),
            "removed_bytes": len(raw) - len(cut),
        }
        _atomic_write(
            self.artifact(lease.guid, "published"),
            json.dumps(doc, sort_keys=True).encode(),
        )

    def handlers(self) -> dict[str, Callable[[Lease], None]]:
        return {
            State.DISCOVERED.value: self.fetch,
            State.FETCHED.value: self.analyze,
            State.ANALYZED.value: self.cut,
            State.CUT.value: self.publish,
        }
