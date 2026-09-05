"""Boundary snapping, the refusal rule, cutting, and chapter-mark shifting.

Step 5 of the rebuild.  The design memo's claim, which this module encodes, is that
the two ways of being wrong are not symmetric:

    a listener forgives twenty seconds of advertising, but not a sentence clipped
    mid-word.

Everything here follows from that.  A detector hands us an interval it believes is an
advertisement.  Its edges are approximate -- they came from a transcript timestamp or a
fingerprint correlation peak, and they are wrong by a few hundred milliseconds in an
unknown direction.  Cutting on an approximate edge clips speech.  So before cutting we
*snap* each edge onto a real acoustic edge (a silence), and:

    **if there is no clean edge within tolerance, the segment is KEPT, not cut.**

That refusal is the whole policy.  It is an explicit, recorded decision -- every
:class:`SnapDecision` and :class:`SegmentDecision` carries the outcome, the reason and
the numbers behind it, and :meth:`CutPlan.log_records` renders them as structlog-style
snake_case fields.  It is deliberately *not* a silent fallback to "cut anyway", because
a silent fallback is indistinguishable, from the outside, from having no policy at all.

Independence note
-----------------
Nothing in this module measures its own output.  ``apply_cuts`` computes sample ranges
and hands them to ffmpeg; the tests verify the resulting duration with **ffprobe**, which
shares no code with the cutter.  Verifying our cut arithmetic with our cut arithmetic
would be circular.

pydub is deliberately not used: ``audioop`` was removed in Python 3.13 and pydub depends
on it.  All audio work is ffmpeg subprocesses.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "DEFAULT_EDGE_GUARD_SECONDS",
    "DEFAULT_MIN_SILENCE_SECONDS",
    "DEFAULT_NOISE_FLOOR_DB",
    "DEFAULT_TOLERANCE_SECONDS",
    "BoundaryError",
    "Chapter",
    "CutPlan",
    "CutResult",
    "RefusalReason",
    "SegmentDecision",
    "ShiftedChapter",
    "Silence",
    "SilenceMap",
    "SnapDecision",
    "SnapOutcome",
    "Word",
    "apply_cuts",
    "as_silence_map",
    "detect_silences",
    "plan_cuts",
    "probe_audio",
    "shift_chapters",
    "snap",
    "snap_segment",
    "word_at",
]


#: Level below which ffmpeg's ``silencedetect`` calls a sample silent.
DEFAULT_NOISE_FLOOR_DB: float = -45.0

#: Shortest run of quiet that counts as an edge.  Anything shorter is a glottal stop, not
#: a boundary between one thing and another.
DEFAULT_MIN_SILENCE_SECONDS: float = 0.10

#: How far from a proposed boundary we are willing to look for a real edge.  Beyond this
#: the "edge" we found is not plausibly the edge of *this* thing.
DEFAULT_TOLERANCE_SECONDS: float = 1.5

#: How far inside a silence a snapped boundary is placed, so that a cut never lands on the
#: exact instant speech stops or starts.
DEFAULT_EDGE_GUARD_SECONDS: float = 0.02


Interval = Tuple[float, float]
PathLike = Union[str, Path]


class BoundaryError(RuntimeError):
    """Raised when boundary work cannot proceed (bad input, missing tool, bad ffmpeg)."""


# --------------------------------------------------------------------------------------
# small validation helpers -- nothing here silently repairs bad input
# --------------------------------------------------------------------------------------


def _finite(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoundaryError(f"{name} must be a number, got {value!r}")
    out = float(value)
    if math.isnan(out) or math.isinf(out):
        raise BoundaryError(f"{name} must be finite, got {out!r}")
    return out


def _non_negative(value: object, *, name: str) -> float:
    out = _finite(value, name=name)
    if out < 0.0:
        raise BoundaryError(f"{name} must be >= 0, got {out}")
    return out


def _normalize_intervals(intervals: Iterable[object], *, name: str) -> List[Interval]:
    """Validate, sort and merge ``intervals`` into disjoint ``(start, end)`` pairs."""
    pairs: List[Interval] = []
    for index, raw in enumerate(intervals):
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or len(raw) != 2:
            raise BoundaryError(f"{name}[{index}] is not a (start, end) pair: {raw!r}")
        start = _non_negative(raw[0], name=f"{name}[{index}].start")
        end = _non_negative(raw[1], name=f"{name}[{index}].end")
        if end < start:
            raise BoundaryError(f"{name}[{index}] has end < start ({end} < {start})")
        if end > start:
            pairs.append((start, end))
    pairs.sort()
    merged: List[Interval] = []
    for start, end in pairs:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# --------------------------------------------------------------------------------------
# ffmpeg / ffprobe plumbing
# --------------------------------------------------------------------------------------


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BoundaryError(f"{name} not found on PATH; boundary work needs it")
    return path


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(list(cmd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise BoundaryError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc


@dataclass(frozen=True)
class AudioInfo:
    """What ffprobe says about an audio file.  Used to convert seconds to samples."""

    path: str
    sample_rate: int
    channels: int
    samples: int
    duration_seconds: float

    @property
    def frame_seconds(self) -> float:
        """Length of one sample frame in seconds -- the resolution of any cut."""
        return 1.0 / self.sample_rate


def probe_audio(path: PathLike) -> AudioInfo:
    """Probe ``path`` with ffprobe.

    ``samples`` is exact for PCM (``duration_ts`` in a ``1/sample_rate`` time base) and
    falls back to ``round(duration * sample_rate)`` for formats where it is not.
    """
    src = Path(path)
    if not src.exists():
        raise BoundaryError(f"no such audio file: {src}")
    out = _run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,duration_ts,time_base:format=duration",
            "-of",
            "default=nw=1",
            str(src),
        ]
    ).stdout
    fields: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    try:
        sample_rate = int(fields["sample_rate"])
        channels = int(fields.get("channels", "1"))
    except (KeyError, ValueError) as exc:
        raise BoundaryError(f"ffprobe gave no usable audio stream for {src}: {out!r}") from exc
    if sample_rate <= 0:
        raise BoundaryError(f"ffprobe reported sample_rate={sample_rate} for {src}")

    duration_ts = fields.get("duration_ts", "N/A")
    time_base = fields.get("time_base", "")
    samples: Optional[int] = None
    if duration_ts not in ("", "N/A") and time_base == f"1/{sample_rate}":
        try:
            samples = int(duration_ts)
        except ValueError:
            samples = None
    duration = fields.get("duration", "N/A")
    duration_seconds: float
    if duration not in ("", "N/A"):
        duration_seconds = float(duration)
    elif samples is not None:
        duration_seconds = samples / sample_rate
    else:
        raise BoundaryError(f"ffprobe reported no duration for {src}")
    if samples is None:
        samples = int(round(duration_seconds * sample_rate))
    return AudioInfo(
        path=str(src),
        sample_rate=sample_rate,
        channels=channels,
        samples=samples,
        duration_seconds=duration_seconds,
    )


# --------------------------------------------------------------------------------------
# silence detection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Silence:
    """One contiguous run of quiet, in seconds."""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise BoundaryError(f"silence has end < start ({self.end} < {self.start})")

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def contains(self, t: float) -> bool:
        return self.start <= t <= self.end

    def distance_to(self, t: float) -> float:
        """Seconds from ``t`` to the nearest point of this region; 0 if inside it."""
        if t < self.start:
            return self.start - t
        if t > self.end:
            return t - self.end
        return 0.0

    def as_tuple(self) -> Interval:
        return (self.start, self.end)


@dataclass(frozen=True)
class SilenceMap:
    """Every silence found in one piece of audio, sorted by start time."""

    regions: Tuple[Silence, ...] = ()
    source: Optional[str] = None
    noise_floor_db: Optional[float] = None
    min_silence_seconds: Optional[float] = None

    def __len__(self) -> int:
        return len(self.regions)

    def __iter__(self):
        return iter(self.regions)

    @classmethod
    def from_intervals(
        cls, intervals: Iterable[object], *, source: Optional[str] = None
    ) -> "SilenceMap":
        merged = _normalize_intervals(intervals, name="silences")
        return cls(regions=tuple(Silence(s, e) for s, e in merged), source=source)

    def candidates(self, boundary: float, tolerance: float) -> List[Silence]:
        """Silences whose nearest point is within ``tolerance`` of ``boundary``.

        Sorted best-first: closest region wins, ties broken toward the region whose
        midpoint is nearest, then by start time (so the result is deterministic).
        """
        near = [r for r in self.regions if r.distance_to(boundary) <= tolerance]
        near.sort(key=lambda r: (r.distance_to(boundary), abs(r.midpoint - boundary), r.start))
        return near


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def detect_silences(
    path: PathLike,
    *,
    noise_floor_db: float = DEFAULT_NOISE_FLOOR_DB,
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
) -> SilenceMap:
    """Find silences in ``path`` using ffmpeg's ``silencedetect`` filter.

    A silence that runs to the end of the file is reported by ffmpeg with a
    ``silence_start`` and no ``silence_end``; it is closed here at the file duration.
    """
    noise_floor_db = _finite(noise_floor_db, name="noise_floor_db")
    min_silence_seconds = _non_negative(min_silence_seconds, name="min_silence_seconds")
    info = probe_audio(path)
    proc = _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_floor_db}dB:d={min_silence_seconds}",
            "-f",
            "null",
            "-",
        ]
    )
    regions: List[Silence] = []
    pending: Optional[float] = None
    for line in proc.stderr.splitlines():
        if "silencedetect" not in line:
            continue
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            end = min(float(end_match.group(1)), info.duration_seconds)
            if end > pending:
                regions.append(Silence(pending, end))
            pending = None
    if pending is not None and info.duration_seconds > pending:
        regions.append(Silence(pending, info.duration_seconds))
    regions.sort(key=lambda r: r.start)
    return SilenceMap(
        regions=tuple(regions),
        source=str(path),
        noise_floor_db=noise_floor_db,
        min_silence_seconds=min_silence_seconds,
    )


SilenceSource = Union[SilenceMap, PathLike, Iterable[object]]


def as_silence_map(audio: SilenceSource, **detect_kwargs) -> SilenceMap:
    """Coerce ``audio`` to a :class:`SilenceMap`.

    Accepts an already-built map, a path to an audio file (which is analysed), or a raw
    iterable of ``(start, end)`` pairs (which is what a test with a known-by-construction
    silence layout supplies).
    """
    if isinstance(audio, SilenceMap):
        return audio
    if isinstance(audio, (str, Path)):
        return detect_silences(audio, **detect_kwargs)
    if detect_kwargs:
        raise BoundaryError(
            "detection options were given but `audio` is not a path: "
            f"{sorted(detect_kwargs)}"
        )
    return SilenceMap.from_intervals(audio)


# --------------------------------------------------------------------------------------
# word timings (optional extra safety net)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    """One word with its timestamps, as a transcriber emits them."""

    start: float
    end: float
    text: str = ""

    def contains_strictly(self, t: float) -> bool:
        """True when ``t`` is strictly inside the word -- i.e. cutting there clips it."""
        return self.start < t < self.end


def _coerce_words(words: Optional[Iterable[object]]) -> Tuple[Word, ...]:
    if words is None:
        return ()
    out: List[Word] = []
    for index, raw in enumerate(words):
        if isinstance(raw, Word):
            out.append(raw)
        elif isinstance(raw, dict):
            out.append(
                Word(
                    _non_negative(raw["start"], name=f"words[{index}].start"),
                    _non_negative(raw["end"], name=f"words[{index}].end"),
                    str(raw.get("text", "")),
                )
            )
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
            out.append(
                Word(
                    _non_negative(raw[0], name=f"words[{index}].start"),
                    _non_negative(raw[1], name=f"words[{index}].end"),
                    str(raw[2]) if len(raw) > 2 else "",
                )
            )
        else:
            raise BoundaryError(f"words[{index}] is not a word: {raw!r}")
    return tuple(out)


def word_at(t: float, words: Iterable[object]) -> Optional[Word]:
    """The word ``t`` falls strictly inside, or ``None``.

    Boundaries exactly on a word's start or end are *not* inside it: cutting at the
    instant a word begins or ends removes nothing of the word.
    """
    for word in _coerce_words(words):
        if word.contains_strictly(t):
            return word
    return None


# --------------------------------------------------------------------------------------
# the snap and the refusal
# --------------------------------------------------------------------------------------


class SnapOutcome(str, Enum):
    """What happened to one proposed boundary."""

    SNAPPED = "snapped"      # moved onto a real edge
    UNCHANGED = "unchanged"  # already sat on a real edge
    REFUSED = "refused"      # no real edge within tolerance -> do not cut here


class RefusalReason(str, Enum):
    """Why a boundary, or a whole segment, was refused.  Recorded, never swallowed."""

    NO_EDGE_WITHIN_TOLERANCE = "no_clean_edge_within_tolerance"
    EDGE_INSIDE_WORD = "candidate_edge_falls_inside_a_word"
    SNAP_EXCEEDS_TOLERANCE = "snap_would_exceed_tolerance"
    START_REFUSED = "start_boundary_refused"
    END_REFUSED = "end_boundary_refused"
    DEGENERATE_AFTER_SNAP = "segment_empty_or_inverted_after_snap"


@dataclass(frozen=True)
class SnapDecision:
    """The result of snapping one boundary: a new position, or a refusal.

    ``snapped`` is ``None`` exactly when the boundary was refused.  Consumers must check
    :attr:`accepted` -- there is no "best effort" position to fall back on, by design.
    """

    requested: float
    tolerance: float
    outcome: SnapOutcome
    snapped: Optional[float] = None
    silence: Optional[Silence] = None
    reason: Optional[str] = None
    searched_regions: int = 0

    @property
    def accepted(self) -> bool:
        return self.outcome in (SnapOutcome.SNAPPED, SnapOutcome.UNCHANGED)

    @property
    def refused(self) -> bool:
        return self.outcome is SnapOutcome.REFUSED

    @property
    def shift(self) -> Optional[float]:
        """``snapped - requested``.  Negative = moved earlier, positive = moved later."""
        if self.snapped is None:
            return None
        return self.snapped - self.requested

    @property
    def direction(self) -> Optional[str]:
        shift = self.shift
        if shift is None:
            return None
        if shift < 0:
            return "earlier"
        if shift > 0:
            return "later"
        return "unchanged"

    def log_fields(self) -> Dict[str, object]:
        """snake_case fields for structlog, matching the house logging style."""
        return {
            "requested": round(self.requested, 6),
            "outcome": self.outcome.value,
            "snapped": None if self.snapped is None else round(self.snapped, 6),
            "shift": None if self.shift is None else round(self.shift, 6),
            "direction": self.direction,
            "tolerance": self.tolerance,
            "reason": self.reason,
            "silence": None if self.silence is None else self.silence.as_tuple(),
            "searched_regions": self.searched_regions,
        }


def snap(
    boundary: float,
    audio: SilenceSource,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
    *,
    words: Optional[Iterable[object]] = None,
    edge_guard_seconds: float = DEFAULT_EDGE_GUARD_SECONDS,
    **detect_kwargs,
) -> SnapDecision:
    """Snap ``boundary`` onto the nearest real acoustic edge, or refuse.

    The snapped point is the point of the nearest silence that is *closest to the
    requested boundary*, held ``edge_guard_seconds`` clear of the silence's own edges.
    The movement is therefore always **toward** the silence: a boundary sitting in speech
    before a pause moves later, one sitting in speech after a pause moves earlier, and a
    boundary already inside the pause does not move at all.  Snapping away from the
    silence would put the cut deeper into speech, which is the error this whole module
    exists to prevent.

    Args:
        boundary: proposed cut edge, in seconds.
        audio: a path (analysed with ffmpeg), a :class:`SilenceMap`, or an iterable of
            known ``(start, end)`` silence intervals.
        tolerance: how far we are willing to move, in seconds.  Must be > 0.
        words: optional word timings; a candidate landing strictly inside a word is
            rejected even if it looked like an edge.
        edge_guard_seconds: how far inside the silence to sit.
        **detect_kwargs: passed to :func:`detect_silences` when ``audio`` is a path.

    Returns:
        A :class:`SnapDecision`.  ``decision.refused`` is True when no clean edge was
        found -- **the caller must then keep the audio, not cut it**.
    """
    boundary = _non_negative(boundary, name="boundary")
    tolerance = _finite(tolerance, name="tolerance")
    if tolerance <= 0.0:
        raise BoundaryError(f"tolerance must be > 0, got {tolerance}")
    edge_guard_seconds = _non_negative(edge_guard_seconds, name="edge_guard_seconds")
    word_list = _coerce_words(words)

    silences = as_silence_map(audio, **detect_kwargs)
    candidates = silences.candidates(boundary, tolerance)
    if not candidates:
        return SnapDecision(
            requested=boundary,
            tolerance=tolerance,
            outcome=SnapOutcome.REFUSED,
            reason=RefusalReason.NO_EDGE_WITHIN_TOLERANCE.value,
            searched_regions=len(silences),
        )

    last_reason = RefusalReason.NO_EDGE_WITHIN_TOLERANCE.value
    for region in candidates:
        guard = min(edge_guard_seconds, region.duration / 2.0)
        low = region.start + guard
        high = region.end - guard
        # Move toward the silence and stop as soon as we are safely inside it.
        target = min(max(boundary, low), high)
        if abs(target - boundary) > tolerance:
            last_reason = RefusalReason.SNAP_EXCEEDS_TOLERANCE.value
            continue
        offender = next((w for w in word_list if w.contains_strictly(target)), None)
        if offender is not None:
            last_reason = RefusalReason.EDGE_INSIDE_WORD.value
            continue
        outcome = SnapOutcome.UNCHANGED if target == boundary else SnapOutcome.SNAPPED
        return SnapDecision(
            requested=boundary,
            tolerance=tolerance,
            outcome=outcome,
            snapped=target,
            silence=region,
            reason="snapped_into_silence" if outcome is SnapOutcome.SNAPPED else "already_on_edge",
            searched_regions=len(silences),
        )

    return SnapDecision(
        requested=boundary,
        tolerance=tolerance,
        outcome=SnapOutcome.REFUSED,
        reason=last_reason,
        searched_regions=len(silences),
    )


# --------------------------------------------------------------------------------------
# segment-level policy: both edges must be clean, or the segment is kept
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentDecision:
    """What we decided to do about one proposed ad segment."""

    requested: Interval
    start_decision: SnapDecision
    end_decision: SnapDecision
    cut: Optional[Interval] = None
    reason: Optional[str] = None

    @property
    def kept(self) -> bool:
        """True when the segment is left in the episode (the refusal outcome)."""
        return self.cut is None

    @property
    def cut_seconds(self) -> float:
        return 0.0 if self.cut is None else self.cut[1] - self.cut[0]

    def log_fields(self) -> Dict[str, object]:
        return {
            "event": "segment_kept" if self.kept else "segment_cut",
            "requested_start": round(self.requested[0], 6),
            "requested_end": round(self.requested[1], 6),
            "cut_start": None if self.cut is None else round(self.cut[0], 6),
            "cut_end": None if self.cut is None else round(self.cut[1], 6),
            "reason": self.reason,
            "start": self.start_decision.log_fields(),
            "end": self.end_decision.log_fields(),
        }


def snap_segment(
    interval: Sequence[float],
    audio: SilenceSource,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
    *,
    words: Optional[Iterable[object]] = None,
    edge_guard_seconds: float = DEFAULT_EDGE_GUARD_SECONDS,
    **detect_kwargs,
) -> SegmentDecision:
    """Snap both edges of one proposed ad segment, applying the refusal rule.

    **If either edge has no clean acoustic edge within tolerance the segment is KEPT.**
    Not shrunk, not cut approximately, not cut with a warning -- kept, with the reason
    recorded on the returned :class:`SegmentDecision`.
    """
    if isinstance(interval, (str, bytes)) or not isinstance(interval, Sequence) or len(interval) != 2:
        raise BoundaryError(f"interval must be a (start, end) pair, got {interval!r}")
    start = _non_negative(interval[0], name="interval.start")
    end = _non_negative(interval[1], name="interval.end")
    if end < start:
        raise BoundaryError(f"interval has end < start ({end} < {start})")

    silences = as_silence_map(audio, **detect_kwargs)
    start_decision = snap(
        start, silences, tolerance, words=words, edge_guard_seconds=edge_guard_seconds
    )
    end_decision = snap(
        end, silences, tolerance, words=words, edge_guard_seconds=edge_guard_seconds
    )

    if start_decision.refused:
        return SegmentDecision(
            requested=(start, end),
            start_decision=start_decision,
            end_decision=end_decision,
            cut=None,
            reason=RefusalReason.START_REFUSED.value,
        )
    if end_decision.refused:
        return SegmentDecision(
            requested=(start, end),
            start_decision=start_decision,
            end_decision=end_decision,
            cut=None,
            reason=RefusalReason.END_REFUSED.value,
        )

    snapped_start = float(start_decision.snapped)
    snapped_end = float(end_decision.snapped)
    if snapped_end <= snapped_start:
        return SegmentDecision(
            requested=(start, end),
            start_decision=start_decision,
            end_decision=end_decision,
            cut=None,
            reason=RefusalReason.DEGENERATE_AFTER_SNAP.value,
        )
    return SegmentDecision(
        requested=(start, end),
        start_decision=start_decision,
        end_decision=end_decision,
        cut=(snapped_start, snapped_end),
        reason="both_edges_clean",
    )


@dataclass(frozen=True)
class CutPlan:
    """Every segment decision for one episode, plus the cuts that survived."""

    segments: Tuple[SegmentDecision, ...] = ()
    silences: Optional[SilenceMap] = None
    tolerance: float = DEFAULT_TOLERANCE_SECONDS

    @property
    def cuts(self) -> List[Interval]:
        """Intervals we will actually remove, merged and sorted."""
        return _normalize_intervals(
            [s.cut for s in self.segments if s.cut is not None], name="cuts"
        )

    @property
    def kept_segments(self) -> List[SegmentDecision]:
        """Segments the refusal rule kept.  Non-empty means we declined to cut."""
        return [s for s in self.segments if s.kept]

    @property
    def total_cut_seconds(self) -> float:
        return float(sum(end - start for start, end in self.cuts))

    @property
    def refusal_count(self) -> int:
        return len(self.kept_segments)

    def log_records(self) -> List[Dict[str, object]]:
        """One structlog-ready record per segment, refusals included."""
        return [s.log_fields() for s in self.segments]


def plan_cuts(
    intervals: Iterable[Sequence[float]],
    audio: SilenceSource,
    tolerance: float = DEFAULT_TOLERANCE_SECONDS,
    *,
    words: Optional[Iterable[object]] = None,
    edge_guard_seconds: float = DEFAULT_EDGE_GUARD_SECONDS,
    **detect_kwargs,
) -> CutPlan:
    """Turn a detector's proposed ad intervals into a cut plan.

    The audio is analysed once, then every segment is snapped against the same map.
    """
    silences = as_silence_map(audio, **detect_kwargs)
    decisions = tuple(
        snap_segment(
            interval,
            silences,
            tolerance,
            words=words,
            edge_guard_seconds=edge_guard_seconds,
        )
        for interval in intervals
    )
    return CutPlan(segments=decisions, silences=silences, tolerance=tolerance)


# --------------------------------------------------------------------------------------
# the cutter
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CutResult:
    """What ``apply_cuts`` did, in samples as well as seconds."""

    source: str
    output: str
    sample_rate: int
    input_samples: int
    removed_samples: int
    kept_ranges: Tuple[Tuple[int, int], ...]
    cuts: Tuple[Interval, ...]

    @property
    def expected_output_samples(self) -> int:
        return self.input_samples - self.removed_samples

    @property
    def expected_output_seconds(self) -> float:
        return self.expected_output_samples / self.sample_rate

    @property
    def removed_seconds(self) -> float:
        return self.removed_samples / self.sample_rate


def apply_cuts(
    source: PathLike,
    output: PathLike,
    cuts: Iterable[Sequence[float]],
    *,
    plan: Optional[CutPlan] = None,
) -> CutResult:
    """Remove ``cuts`` from ``source``, writing ``output``.

    Cuts are converted to whole sample indices and handed to ffmpeg's ``atrim`` using
    ``start_sample``/``end_sample``, so every edit lands on a frame boundary and the
    output length is exactly ``input - sum(cuts)`` samples.  The kept pieces are joined
    with the ``concat`` filter; nothing is resampled.

    Passing ``plan`` instead of raw ``cuts`` is the normal path -- use
    ``apply_cuts(src, dst, plan.cuts)``; ``plan`` here is accepted only so the result can
    record it.  Segments the plan refused are simply absent from ``plan.cuts`` and so are
    left in the audio, which is the point.
    """
    src = Path(source)
    dst = Path(output)
    info = probe_audio(src)
    normalized = _normalize_intervals(cuts, name="cuts")

    ranges: List[Tuple[int, int]] = []
    for start, end in normalized:
        lo = max(0, min(info.samples, int(round(start * info.sample_rate))))
        hi = max(0, min(info.samples, int(round(end * info.sample_rate))))
        if hi > lo:
            ranges.append((lo, hi))
    # merge again in sample space: two cuts can round onto the same frame
    merged: List[Tuple[int, int]] = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))

    kept: List[Tuple[int, int]] = []
    cursor = 0
    for lo, hi in merged:
        if lo > cursor:
            kept.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < info.samples:
        kept.append((cursor, info.samples))
    if not kept:
        raise BoundaryError(
            f"cuts cover the whole of {src} ({info.samples} samples); refusing to write "
            "an empty file"
        )

    filters = []
    labels = []
    for index, (lo, hi) in enumerate(kept):
        label = f"k{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[0:a]atrim=start_sample={lo}:end_sample={hi},asetpts=N/SR/TB[{label}]"
        )
    if len(kept) == 1:
        graph = ";".join(filters)
        out_label = labels[0]
    else:
        graph = ";".join(filters + ["".join(labels) + f"concat=n={len(kept)}:v=0:a=1[out]"])
        out_label = "[out]"

    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(src),
            "-filter_complex",
            graph,
            "-map",
            out_label,
            "-ar",
            str(info.sample_rate),
            "-ac",
            str(info.channels),
            "-c:a",
            "pcm_s16le",
            "-fflags",
            "+bitexact",
            "-flags",
            "+bitexact",
            "-map_metadata",
            "-1",
            str(dst),
        ]
    )

    removed = sum(hi - lo for lo, hi in merged)
    return CutResult(
        source=str(src),
        output=str(dst),
        sample_rate=info.sample_rate,
        input_samples=info.samples,
        removed_samples=removed,
        kept_ranges=tuple(kept),
        cuts=tuple((lo / info.sample_rate, hi / info.sample_rate) for lo, hi in merged),
    )


# --------------------------------------------------------------------------------------
# chapter marks
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Chapter:
    """A chapter mark: a title at an instant in the original timeline."""

    start: float
    title: str = ""


@dataclass(frozen=True)
class ShiftedChapter:
    """A chapter mark after cutting."""

    start: float
    title: str
    original_start: float
    removed_before: float
    dropped: bool = False

    @property
    def shift(self) -> float:
        return self.start - self.original_start


def _coerce_chapters(chapters: Iterable[object]) -> List[Chapter]:
    out: List[Chapter] = []
    for index, raw in enumerate(chapters):
        if isinstance(raw, Chapter):
            out.append(raw)
        elif isinstance(raw, dict):
            out.append(
                Chapter(
                    _non_negative(raw["start"], name=f"chapters[{index}].start"),
                    str(raw.get("title", "")),
                )
            )
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            out.append(Chapter(_non_negative(raw, name=f"chapters[{index}]")))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 1:
            out.append(
                Chapter(
                    _non_negative(raw[0], name=f"chapters[{index}].start"),
                    str(raw[1]) if len(raw) > 1 else "",
                )
            )
        else:
            raise BoundaryError(f"chapters[{index}] is not a chapter: {raw!r}")
    return out


def shift_chapters(
    chapters: Iterable[object],
    cuts: Iterable[Sequence[float]],
    *,
    drop_inside_cuts: bool = True,
) -> List[ShiftedChapter]:
    """Move chapter marks onto the cut timeline.

    A chapter at ``T`` loses every second that was removed before it: after a cut of
    duration ``D`` that *ends before* ``T``, the chapter lands at ``T - D``.  Cuts that
    start after ``T`` do not move it at all.

    A chapter that falls strictly inside a cut has no place left in the output.  With
    ``drop_inside_cuts`` (the default) it is dropped; otherwise it collapses onto the
    start of the cut.  Either way it is flagged ``dropped``.
    """
    normalized = _normalize_intervals(cuts, name="cuts")
    out: List[ShiftedChapter] = []
    for chapter in _coerce_chapters(chapters):
        removed = 0.0
        inside = False
        for lo, hi in normalized:
            if hi <= chapter.start:
                removed += hi - lo          # the whole cut is behind us
            elif lo < chapter.start < hi:
                removed += chapter.start - lo  # we are standing in the middle of it
                inside = True
            # cuts starting at or after the chapter do not shift it
        shifted = ShiftedChapter(
            start=chapter.start - removed,
            title=chapter.title,
            original_start=chapter.start,
            removed_before=removed,
            dropped=inside,
        )
        if inside and drop_inside_cuts:
            continue
        out.append(shifted)
    return out
