"""Dynamic-ad-insertion oracle: exact inserted regions from a clean and a stitched MP3.

Podcast hosts serve different bytes to different clients.  A plain HTTP fetch of a Lage
der Nation or Solved enclosure returns the clean master; a fetch with a podcatcher
``User-Agent`` returns a *variation* with advertising stitched in.  Measured on
2026-09-06, the stitched files reuse the clean file's MP3 frames **byte for byte** and
only insert new frames, so the inserted regions can be recovered exactly by walking
both frame sequences in lockstep.  Nobody judged anything; the boundaries are known
because we can see the splice.

That makes this the highest-grade ground truth available for real episodes: exact to
one MP3 frame (26 ms at 44.1 kHz), independent of any transcript and of any listener.

Independence note: the sum of inserted durations must equal the difference between the
two files' durations, which the tests check with ``ffprobe`` -- a tool that shares no
code with this module.

Usage::

    python -m podcleaner.eval.dai clean.mp3 stitched.mp3
"""

from __future__ import annotations

import argparse
import json
import mmap
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple, Union

__all__ = [
    "DaiError",
    "DaiResult",
    "Frame",
    "InsertedRegion",
    "find_inserted_regions",
    "id3v2_size",
    "iter_frames",
    "parse_frame_header",
]

PathLike = Union[str, Path]


class DaiError(RuntimeError):
    """Raised when the two files cannot be reconciled frame by frame."""


# --------------------------------------------------------------------------------------
# MP3 frame parsing
# --------------------------------------------------------------------------------------

# index: 0 = MPEG 2.5, 1 = reserved, 2 = MPEG 2, 3 = MPEG 1
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
# Layer III bitrates (kbps); index 0 = free, 15 = bad
_BITRATES_L3 = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_BITRATES_L3[0] = _BITRATES_L3[2]


@dataclass(frozen=True)
class Frame:
    offset: int
    length: int
    samples: int
    sample_rate: int
    bitrate_kbps: int

    @property
    def duration(self) -> float:
        return self.samples / self.sample_rate

    @property
    def end(self) -> int:
        return self.offset + self.length


def id3v2_size(buf) -> int:
    """Size of a leading ID3v2 tag (0 if none), including footer when flagged."""
    if len(buf) < 10 or bytes(buf[:3]) != b"ID3":
        return 0
    s = buf[6:10]
    size = (s[0] << 21) | (s[1] << 14) | (s[2] << 7) | s[3]
    footer = 10 if (buf[5] & 0x10) else 0
    return 10 + size + footer


def parse_frame_header(buf, off: int) -> Optional[Frame]:
    """Parse an MPEG Layer III frame header at ``off``; ``None`` if not a valid one."""
    if off + 4 > len(buf):
        return None
    b0, b1, b2, _b3 = buf[off], buf[off + 1], buf[off + 2], buf[off + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != 1:  # reserved version, or not Layer III
        return None
    bitrate_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = _BITRATES_L3[version][bitrate_idx]
    sample_rate = _SAMPLE_RATES[version][sr_idx]
    samples = 1152 if version == 3 else 576
    length = (samples // 8) * bitrate * 1000 // sample_rate + padding
    if length < 24:
        return None
    return Frame(off, length, samples, sample_rate, bitrate)


def _looks_like_frame_at(buf, off: int, depth: int = 2) -> bool:
    """A header is only trusted when the frames it predicts also parse -- a lone sync
    word inside audio data is common; two consecutive consistent ones are not."""
    f = parse_frame_header(buf, off)
    for _ in range(depth):
        if f is None:
            return False
        if f.end >= len(buf):
            return True
        f = parse_frame_header(buf, f.end)
    return f is not None


def iter_frames(buf, start: int = 0) -> Iterator[Frame]:
    """Yield frames from ``start``, resynchronising after garbage.  Stops at an ID3v1
    ``TAG`` trailer or when fewer than 4 bytes remain."""
    n = len(buf)
    off = start
    while off + 4 <= n:
        if n - off == 128 and bytes(buf[off : off + 3]) == b"TAG":
            return
        f = parse_frame_header(buf, off)
        if f is not None and (f.end >= n or _looks_like_frame_at(buf, f.end, 1)):
            yield f
            off = f.end
            continue
        # resync: scan forward for the next trustworthy header
        nxt = off + 1
        while nxt + 4 <= n and not _looks_like_frame_at(buf, nxt):
            nxt += 1
        if nxt + 4 > n:
            return
        off = nxt


def _is_info_frame(buf, f: Frame) -> bool:
    body = bytes(buf[f.offset : f.end])
    return b"Xing" in body or b"Info" in body


# --------------------------------------------------------------------------------------
# lockstep walk
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InsertedRegion:
    """A span of the *stitched* file that has no counterpart in the clean file."""

    start: float
    end: float
    start_frame: int
    end_frame: int
    byte_start: int
    byte_end: int
    #: clean-file frames that were skipped (re-encoded at the splice) to resume matching,
    #: and their duration.  They lie inside the region but are not inserted audio.
    skipped_clean_frames: int = 0
    skipped_clean_seconds: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "skipped_clean_frames": self.skipped_clean_frames,
            "skipped_clean_seconds": round(self.skipped_clean_seconds, 3),
        }


@dataclass
class DaiResult:
    regions: List[InsertedRegion]
    clean_duration: float
    stitched_duration: float
    clean_frames: int
    stitched_frames: int
    matched_frames: int
    #: frames that differ in place (same position in both streams), typically the
    #: encoder rewriting the frame at a splice.  Not counted as inserted.
    modified_frames: int = 0
    clean_path: Optional[str] = None
    stitched_path: Optional[str] = None
    #: ``(clean_time, shift)`` breakpoints: for clean time >= clean_time the stitched
    #: time is ``clean_time + shift``.
    offset_map: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def total_inserted(self) -> float:
        return sum(r.duration for r in self.regions)

    @property
    def duration_delta(self) -> float:
        """What the inserted total *should* be, by the two files' own durations."""
        return self.stitched_duration - self.clean_duration

    @property
    def skipped_clean_seconds(self) -> float:
        return sum(r.skipped_clean_seconds for r in self.regions)

    @property
    def reconciled_inserted(self) -> float:
        """``total_inserted`` minus clean frames rewritten at splices.  Equals
        :attr:`duration_delta` exactly when the two files are clean-plus-insertions."""
        return self.total_inserted - self.skipped_clean_seconds

    def intervals(self) -> List[Tuple[float, float]]:
        return [(r.start, r.end) for r in self.regions]

    def to_stitched(self, clean_t: float) -> float:
        shift = 0.0
        for at, s in self.offset_map:
            if clean_t >= at:
                shift = s
            else:
                break
        return clean_t + shift

    def to_clean(self, stitched_t: float) -> Optional[float]:
        """Clean-file time for a stitched-file time, or ``None`` inside an insert."""
        for r in self.regions:
            if r.start <= stitched_t < r.end:
                return None
        shift = 0.0
        for r in self.regions:
            if r.end <= stitched_t:
                shift += r.duration
        return stitched_t - shift

    def to_dict(self) -> dict:
        return {
            "schema": "podcleaner.dai/1",
            "clean_path": self.clean_path,
            "stitched_path": self.stitched_path,
            "clean_duration": round(self.clean_duration, 3),
            "stitched_duration": round(self.stitched_duration, 3),
            "duration_delta": round(self.duration_delta, 3),
            "total_inserted": round(self.total_inserted, 3),
            "skipped_clean_seconds": round(self.skipped_clean_seconds, 3),
            "reconciled_inserted": round(self.reconciled_inserted, 3),
            "clean_frames": self.clean_frames,
            "stitched_frames": self.stitched_frames,
            "matched_frames": self.matched_frames,
            "modified_frames": self.modified_frames,
            "regions": [r.to_dict() for r in self.regions],
        }


def _frames_of(buf) -> List[Frame]:
    start = id3v2_size(buf)
    frames = list(iter_frames(buf, start))
    if not frames:
        raise DaiError("no MP3 frames found")
    # Drop a leading Xing/Info header frame: it carries no audio and the stitcher
    # writes its own.
    if _is_info_frame(buf, frames[0]):
        frames = frames[1:]
    return frames


def _same(bc, fc: Frame, bs, fs: Frame) -> bool:
    return fc.length == fs.length and bc[fc.offset : fc.end] == bs[fs.offset : fs.end]


def _run_matches(bc, fcs: Sequence[Frame], i: int, bs, fss: Sequence[Frame], j: int, run: int) -> bool:
    if i + run > len(fcs) or j + run > len(fss):
        # near the end: require whatever remains to match
        run = min(len(fcs) - i, len(fss) - j)
        if run <= 0:
            return False
    for d in range(run):
        if not _same(bc, fcs[i + d], bs, fss[j + d]):
            return False
    return True


def find_inserted_regions(
    clean: PathLike,
    stitched: PathLike,
    *,
    min_match_run: int = 8,
    max_skip_clean: int = 4,
) -> DaiResult:
    """Walk both files frame by frame and return every stitched-only region.

    ``min_match_run`` consecutive identical frames are required to declare that the
    clean stream has resumed, so identical *silent* frames inside an advertisement
    cannot end a region early.  Up to ``max_skip_clean`` clean frames may be skipped
    at a splice, because a stitcher may re-encode the frame adjacent to the cut.
    """
    clean_p, stitched_p = Path(clean), Path(stitched)
    with clean_p.open("rb") as fc, stitched_p.open("rb") as fs:
        bc = mmap.mmap(fc.fileno(), 0, access=mmap.ACCESS_READ)
        bs = mmap.mmap(fs.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            return _walk(bc, bs, min_match_run, max_skip_clean, str(clean_p), str(stitched_p))
        finally:
            bc.close()
            bs.close()


def _walk(bc, bs, run: int, max_skip: int, clean_path: str, stitched_path: str) -> DaiResult:
    fcs = _frames_of(bc)
    fss = _frames_of(bs)
    # cumulative start time of every stitched frame (index len(fss) == total duration)
    stitched_t = [0.0] * (len(fss) + 1)
    for k, f in enumerate(fss):
        stitched_t[k + 1] = stitched_t[k] + f.duration
    clean_duration = sum(f.duration for f in fcs)

    regions: List[InsertedRegion] = []
    i = j = 0
    matched = modified = 0
    while i < len(fcs) and j < len(fss):
        if _same(bc, fcs[i], bs, fss[j]):
            i += 1
            j += 1
            matched += 1
            continue
        # Same position, different bytes: a frame rewritten in place?
        in_place = None
        for d in range(1, max_skip + 1):
            if _run_matches(bc, fcs, i + d, bs, fss, j + d, run):
                in_place = d
                break
        if in_place is not None:
            i += in_place
            j += in_place
            modified += in_place
            continue
        # An insertion begins at stitched frame j.  Find where the clean stream
        # resumes: the first stitched frame k > j from which `run` frames match the
        # clean stream at i (allowing a few clean frames to have been rewritten).
        found = None
        for k in range(j + 1, len(fss)):
            for d in range(0, max_skip + 1):
                if i + d < len(fcs) and _same(bc, fcs[i + d], bs, fss[k]) and _run_matches(
                    bc, fcs, i + d, bs, fss, k, run
                ):
                    found = (k, d)
                    break
            if found:
                break
        if found is None:
            # Everything remaining in the stitched file is inserted, but only if the
            # clean stream is (nearly) exhausted -- otherwise the files do not share
            # content the way this oracle assumes.
            if len(fcs) - i > max_skip:
                raise DaiError(
                    f"clean stream diverges at frame {i} of {len(fcs)} "
                    f"(t={sum(f.duration for f in fcs[:i]):.2f}s) and never resumes; "
                    f"the stitched file is not the clean file plus insertions"
                )
            regions.append(
                InsertedRegion(
                    stitched_t[j], stitched_t[len(fss)], j, len(fss),
                    fss[j].offset, fss[-1].end, skipped_clean_frames=len(fcs) - i,
                    skipped_clean_seconds=sum(f.duration for f in fcs[i:]),
                )
            )
            i = len(fcs)
            j = len(fss)
            break
        k, d = found
        regions.append(
            InsertedRegion(
                stitched_t[j], stitched_t[k], j, k, fss[j].offset, fss[k].offset,
                skipped_clean_frames=d, skipped_clean_seconds=sum(f.duration for f in fcs[i : i + d]),
            )
        )
        i += d
        j = k
    if j < len(fss) and i >= len(fcs):
        # post-roll: stitched frames after the clean stream ended
        regions.append(
            InsertedRegion(stitched_t[j], stitched_t[len(fss)], j, len(fss), fss[j].offset, fss[-1].end)
        )
    elif i < len(fcs):
        raise DaiError(
            f"stitched file ended with {len(fcs) - i} clean frames unmatched; "
            f"the stitched file is not the clean file plus insertions"
        )

    result = DaiResult(
        regions=regions,
        clean_duration=clean_duration,
        stitched_duration=stitched_t[len(fss)],
        clean_frames=len(fcs),
        stitched_frames=len(fss),
        matched_frames=matched,
        modified_frames=modified,
        clean_path=clean_path,
        stitched_path=stitched_path,
    )
    shift = 0.0
    for r in regions:
        clean_at = r.start - shift
        shift += r.duration
        result.offset_map.append((clean_at, shift))
    return result


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _clock(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    frac = int(round((t - int(t)) * 1000))
    return f"{h}:{m:02d}:{s:02d}.{frac:03d}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("clean")
    p.add_argument("stitched")
    p.add_argument("--json", metavar="FILE", help="write the result as JSON")
    args = p.parse_args(argv)
    try:
        res = find_inserted_regions(args.clean, args.stitched)
    except DaiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"clean    {_clock(res.clean_duration)}  ({res.clean_frames} frames)")
    print(f"stitched {_clock(res.stitched_duration)}  ({res.stitched_frames} frames)")
    print(f"matched  {res.matched_frames} frames, {res.modified_frames} rewritten in place")
    print(f"inserted {_clock(res.total_inserted)} in {len(res.regions)} region(s); "
          f"reconciled {res.reconciled_inserted:.3f}s vs duration delta {res.duration_delta:.3f}s")
    for r in res.regions:
        print(f"  {_clock(r.start)} -> {_clock(r.end)}  {r.duration:7.2f}s  "
              f"frames {r.start_frame}-{r.end_frame}  skipped_clean={r.skipped_clean_frames}")
    if args.json:
        Path(args.json).write_text(json.dumps(res.to_dict(), indent=2))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
