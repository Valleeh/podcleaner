"""Evaluation harness for PodCleaner (step 1 of the rebuild).

Contains:

* :mod:`podcleaner.eval.scoring`  -- an asymmetric scorer over ad intervals.
* :mod:`podcleaner.eval.corpus`   -- a deterministic synthetic corpus generator whose
  ground truth is exact *by construction*.
* :mod:`podcleaner.eval.label_cli` -- a CLI for a human to label real episodes.

Nothing in this package fabricates labels. ``corpus/real/`` ships empty.
"""

from podcleaner.eval.scoring import (  # noqa: F401
    FALSE_CUT_WEIGHT,
    IntervalError,
    ScoreResult,
    intervals_from_segments,
    intervals_from_transcript,
    normalize_intervals,
    score,
    total_duration,
)

__all__ = [
    "FALSE_CUT_WEIGHT",
    "IntervalError",
    "ScoreResult",
    "intervals_from_segments",
    "intervals_from_transcript",
    "normalize_intervals",
    "score",
    "total_duration",
]
