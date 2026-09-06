"""Asymmetric scorer over ad intervals.

The whole point of this module is that the two ways of being wrong are *not* equally
bad:

* a **missed ad** leaves an advertisement in the episode -- annoying;
* a **false cut** removes real content -- destructive, and unrecoverable for the
  listener.

So the combined score weights false cuts by :data:`FALSE_CUT_WEIGHT`.  The weight is a
named module constant precisely so that it is visible, testable and mutable in one
place; a weight of ``1`` would mean "no asymmetry", which is a different (and, for this
project, wrong) policy.

All intervals are ``[start, end]`` pairs of seconds, half-open in spirit (``end`` is
exclusive) but that only matters for zero-length intervals, which contribute nothing.

Nothing here silently repairs bad input.  Invalid input raises :class:`IntervalError`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

__all__ = [
    "FALSE_CUT_WEIGHT",
    "IntervalError",
    "ScoreResult",
    "difference",
    "intersection",
    "normalize_intervals",
    "score",
    "total_duration",
]


#: How many times worse one second of *false cut* (content wrongly removed) is than one
#: second of *missed ad* (advertisement wrongly kept).  Must be > 1 for the scorer to be
#: asymmetric at all.
FALSE_CUT_WEIGHT: float = 3.0


Interval = Tuple[float, float]


class IntervalError(ValueError):
    """Raised for structurally invalid interval input.

    Subclasses :class:`ValueError` so ``pytest.raises(ValueError)`` also catches it.
    """


# --------------------------------------------------------------------------------------
# validation + normalisation
# --------------------------------------------------------------------------------------


def _coerce_endpoint(value: object, *, kind: str, index: int, which: str) -> float:
    """Return ``value`` as a finite float or raise :class:`IntervalError`.

    ``bool`` is rejected even though it is an ``int`` subclass: ``[True, False]`` is
    almost certainly a bug at the call site, not an interval.
    """
    if isinstance(value, bool):
        raise IntervalError(
            f"{kind}[{index}].{which} is a bool ({value!r}); expected a number"
        )
    if not isinstance(value, (int, float)):
        raise IntervalError(
            f"{kind}[{index}].{which} is non-numeric ({value!r}: {type(value).__name__})"
        )
    as_float = float(value)
    if math.isnan(as_float):
        raise IntervalError(f"{kind}[{index}].{which} is NaN")
    if math.isinf(as_float):
        raise IntervalError(f"{kind}[{index}].{which} is infinite ({as_float})")
    return as_float


def _validate_pair(raw: object, *, kind: str, index: int) -> Interval:
    """Validate one raw interval into a ``(start, end)`` float pair."""
    if isinstance(raw, (str, bytes)):
        raise IntervalError(f"{kind}[{index}] is a string ({raw!r}); expected a pair")
    if not isinstance(raw, Sequence):
        raise IntervalError(
            f"{kind}[{index}] is not a sequence ({raw!r}: {type(raw).__name__})"
        )
    if len(raw) != 2:
        raise IntervalError(
            f"{kind}[{index}] has {len(raw)} element(s); expected exactly 2"
        )
    start = _coerce_endpoint(raw[0], kind=kind, index=index, which="start")
    end = _coerce_endpoint(raw[1], kind=kind, index=index, which="end")
    if end < start:
        raise IntervalError(f"{kind}[{index}] has end < start ({end} < {start})")
    if start < 0.0:
        raise IntervalError(f"{kind}[{index}] has a negative start ({start})")
    return (start, end)


def normalize_intervals(
    intervals: Iterable[object],
    *,
    allow_overlap: bool = True,
    kind: str = "intervals",
) -> List[Interval]:
    """Validate, sort and merge ``intervals``.

    Returns a list of disjoint, non-touching intervals sorted by start time.

    Touching intervals merge: ``[[0, 5], [5, 9]]`` -> ``[(0.0, 9.0)]``.  There is no gap
    between them, so representing them separately carries no information.

    Args:
        intervals: iterable of ``(start, end)`` pairs.
        allow_overlap: if ``False``, genuinely overlapping inputs raise instead of
            merging.  Used for *gold* labels, where an overlap means the label file is
            broken and scoring against it would be meaningless.
        kind: name used in error messages (e.g. ``"gold"``).

    Raises:
        IntervalError: on any structurally invalid input, or on overlap when
            ``allow_overlap`` is ``False``.
    """
    if isinstance(intervals, (str, bytes)):
        raise IntervalError(f"{kind} is a string ({intervals!r}); expected a sequence")
    try:
        raw_list = list(intervals)
    except TypeError as exc:  # not iterable at all
        raise IntervalError(f"{kind} is not iterable ({intervals!r})") from exc

    pairs = [_validate_pair(raw, kind=kind, index=i) for i, raw in enumerate(raw_list)]
    pairs.sort(key=lambda p: (p[0], p[1]))

    merged: List[Interval] = []
    for start, end in pairs:
        if not merged:
            merged.append((start, end))
            continue
        cur_start, cur_end = merged[-1]
        if not allow_overlap and cur_end > start:
            raise IntervalError(
                f"{kind} contains overlapping intervals: "
                f"({cur_start}, {cur_end}) and ({start}, {end})"
            )
        # NOTE: `>=` and not `>`.  Touching intervals ([0,5],[5,9]) must merge -- there
        # is no gap between them.  Mutating this to `>` is mutation (c) in the contract.
        if cur_end >= start:
            merged[-1] = (cur_start, max(cur_end, end))
        else:
            merged.append((start, end))
    return merged


def total_duration(intervals: Sequence[Interval]) -> float:
    """Total seconds covered by already-normalised ``intervals``."""
    return float(sum(end - start for start, end in intervals))


def intersection(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """Intersection of two normalised interval lists."""
    out: List[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def difference(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """``a`` minus ``b``, for two normalised interval lists."""
    out: List[Interval] = []
    for start, end in a:
        cursor = start
        for b_start, b_end in b:
            if b_end <= cursor:
                continue
            if b_start >= end:
                break
            if b_start > cursor:
                out.append((cursor, min(b_start, end)))
            cursor = max(cursor, b_end)
            if cursor >= end:
                break
        if cursor < end:
            out.append((cursor, end))
    return out


# --------------------------------------------------------------------------------------
# the score itself
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of scoring one episode's predicted ad intervals against gold."""

    #: Seconds of real advertisement the predictor failed to cut.
    missed_ad_seconds: float
    #: Seconds of real content the predictor wrongly cut.  The expensive kind of error.
    false_cut_seconds: float
    #: Seconds of advertisement correctly cut.
    caught_ad_seconds: float
    #: Total seconds of advertisement in the gold labels.
    gold_ad_seconds: float
    #: Total seconds the predictor proposed to cut.
    predicted_cut_seconds: float
    #: ``missed_ad_seconds + FALSE_CUT_WEIGHT * false_cut_seconds``.  Lower is better;
    #: 0.0 is perfect.
    combined_score: float
    #: The weight actually used, recorded so a stored result is self-describing.
    false_cut_weight: float = field(default=FALSE_CUT_WEIGHT)

    @property
    def recall(self) -> float:
        """Fraction of gold ad seconds that were cut.  1.0 when there are no ads."""
        if self.gold_ad_seconds == 0.0:
            return 1.0
        return self.caught_ad_seconds / self.gold_ad_seconds

    @property
    def precision(self) -> float:
        """Fraction of cut seconds that were really ads.  1.0 when nothing was cut."""
        if self.predicted_cut_seconds == 0.0:
            return 1.0
        return self.caught_ad_seconds / self.predicted_cut_seconds

    def to_dict(self) -> dict:
        return {
            "missed_ad_seconds": self.missed_ad_seconds,
            "false_cut_seconds": self.false_cut_seconds,
            "caught_ad_seconds": self.caught_ad_seconds,
            "gold_ad_seconds": self.gold_ad_seconds,
            "predicted_cut_seconds": self.predicted_cut_seconds,
            "combined_score": self.combined_score,
            "false_cut_weight": self.false_cut_weight,
            "recall": self.recall,
            "precision": self.precision,
        }


def score(
    predicted: Iterable[object],
    gold: Iterable[object],
    *,
    false_cut_weight: float = FALSE_CUT_WEIGHT,
) -> ScoreResult:
    """Score ``predicted`` ad intervals against ``gold`` ad intervals.

    ``gold`` is held to a stricter standard than ``predicted``: overlapping *gold*
    intervals raise, because gold is supposed to be a clean ground truth, whereas a
    detector emitting overlapping guesses is merely sloppy and gets merged.

    Raises:
        IntervalError: on invalid input.  Never scores invalid input silently.
    """
    if isinstance(false_cut_weight, bool) or not isinstance(
        false_cut_weight, (int, float)
    ):
        raise IntervalError(f"false_cut_weight must be a number, got {false_cut_weight!r}")
    if math.isnan(float(false_cut_weight)) or float(false_cut_weight) < 0.0:
        raise IntervalError(f"false_cut_weight must be >= 0, got {false_cut_weight!r}")

    gold_norm = normalize_intervals(gold, allow_overlap=False, kind="gold")
    pred_norm = normalize_intervals(predicted, allow_overlap=True, kind="predicted")

    caught = total_duration(intersection(pred_norm, gold_norm))
    missed = total_duration(difference(gold_norm, pred_norm))
    false_cut = total_duration(difference(pred_norm, gold_norm))

    # The asymmetry lives here and nowhere else.  Swapping the two terms is mutation (a)
    # in the verification contract; setting the weight to 1 is mutation (b).
    combined = missed + float(false_cut_weight) * false_cut

    return ScoreResult(
        missed_ad_seconds=missed,
        false_cut_seconds=false_cut,
        caught_ad_seconds=caught,
        gold_ad_seconds=total_duration(gold_norm),
        predicted_cut_seconds=total_duration(pred_norm),
        combined_score=combined,
        false_cut_weight=float(false_cut_weight),
    )
