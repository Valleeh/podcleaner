"""Per-episode evaluation of predicted ad cuts against labelled ground truth.

Wraps :mod:`podcleaner.eval.scoring` (the asymmetric interval scorer) with the three
things a real-episode label needs that a synthetic corpus does not:

* **Don't-care regions.**  A labeller may flag a segment as ambiguous (a guest plugging
  their own show, closing credits).  A prediction covering it is neither rewarded nor
  punished, so a judgement call cannot swing the score.
* **Edge tolerance.**  Cue-aligned boundaries are a second or two off the true edge in
  either direction.  Content cut *within* ``edge_tolerance`` of a gold edge is still
  reported as a false cut, but separately from content cut in the middle of editorial,
  which is the failure that quality goal 1 forbids outright.
* **Per-segment matching.**  Seconds explain how much; per-segment start/end deltas
  explain why.  A model can score well overall while clipping every boundary, or find
  every ad but merge two into one -- only the per-segment view shows it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from podcleaner.eval.scoring import (
    FALSE_CUT_WEIGHT,
    Interval,
    difference,
    intersection,
    normalize_intervals,
    total_duration,
)

__all__ = ["GoldAd", "SegmentMatch", "AdEvaluation", "evaluate"]


@dataclass(frozen=True)
class GoldAd:
    start: float
    end: float
    category: str = "sponsor_read"
    ambiguous: bool = False
    note: str = ""

    @property
    def interval(self) -> Interval:
        return (self.start, self.end)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SegmentMatch:
    gold: GoldAd
    found: bool
    #: fraction of the gold segment covered by the union of overlapping predictions
    coverage: float
    start_delta: Optional[float] = None
    end_delta: Optional[float] = None
    #: other gold segments that share a prediction with this one (a merge)
    merged_with: int = 0
    #: number of predictions that together cover this gold segment (a stacked break the
    #: labeller could not split, e.g. one server insert carrying two spots)
    split_into: int = 1

    def to_dict(self) -> dict:
        return {
            "start": round(self.gold.start, 3),
            "end": round(self.gold.end, 3),
            "category": self.gold.category,
            "note": self.gold.note,
            "found": self.found,
            "coverage": round(self.coverage, 3),
            "start_delta": None if self.start_delta is None else round(self.start_delta, 3),
            "end_delta": None if self.end_delta is None else round(self.end_delta, 3),
            "merged_with": self.merged_with,
            "split_into": self.split_into,
        }


@dataclass
class AdEvaluation:
    gold_seconds: float
    predicted_seconds: float
    caught_seconds: float
    missed_seconds: float
    false_cut_seconds: float
    false_cut_outside_tolerance_seconds: float
    dont_care_seconds: float
    edge_tolerance: float
    segments: List[SegmentMatch] = field(default_factory=list)
    spurious: List[Interval] = field(default_factory=list)
    #: the parts of ``spurious`` more than ``edge_tolerance`` from any gold edge
    spurious_outside: List[Interval] = field(default_factory=list)
    false_cut_weight: float = FALSE_CUT_WEIGHT
    #: Gold seconds that lie under some transcript cue.  A cue-aligned classifier cannot
    #: cut the jingle or silence after an ad's last spoken word, so recall is also reported
    #: relative to this ("coverable") amount.  Equals ``gold_seconds`` when no cue spans
    #: were supplied.
    coverable_gold_seconds: Optional[float] = None
    caught_coverable_seconds: Optional[float] = None

    @property
    def recall(self) -> float:
        return 1.0 if self.gold_seconds == 0 else self.caught_seconds / self.gold_seconds

    @property
    def coverable_recall(self) -> float:
        """Recall over the part of the gold ads that a transcript cue actually covers."""
        if self.coverable_gold_seconds is None:
            return self.recall
        if self.coverable_gold_seconds == 0:
            return 1.0
        return (self.caught_coverable_seconds or 0.0) / self.coverable_gold_seconds

    @property
    def precision(self) -> float:
        return 1.0 if self.predicted_seconds == 0 else self.caught_seconds / self.predicted_seconds

    @property
    def combined_score(self) -> float:
        """``missed + FALSE_CUT_WEIGHT * false_cut`` -- lower is better, 0 is perfect."""
        return self.missed_seconds + self.false_cut_weight * self.false_cut_seconds

    @property
    def segments_found(self) -> int:
        return sum(1 for s in self.segments if s.found)

    def to_dict(self) -> dict:
        return {
            "gold_seconds": round(self.gold_seconds, 3),
            "predicted_seconds": round(self.predicted_seconds, 3),
            "caught_seconds": round(self.caught_seconds, 3),
            "missed_seconds": round(self.missed_seconds, 3),
            "false_cut_seconds": round(self.false_cut_seconds, 3),
            "false_cut_outside_tolerance_seconds": round(self.false_cut_outside_tolerance_seconds, 3),
            "dont_care_seconds": round(self.dont_care_seconds, 3),
            "edge_tolerance": self.edge_tolerance,
            "recall": round(self.recall, 4),
            "coverable_gold_seconds": None if self.coverable_gold_seconds is None else round(self.coverable_gold_seconds, 3),
            "coverable_recall": round(self.coverable_recall, 4),
            "precision": round(self.precision, 4),
            "combined_score": round(self.combined_score, 3),
            "false_cut_weight": self.false_cut_weight,
            "segments_total": len(self.segments),
            "segments_found": self.segments_found,
            "segments": [s.to_dict() for s in self.segments],
            "spurious": [[round(a, 3), round(b, 3)] for a, b in self.spurious],
            "spurious_outside": [[round(a, 3), round(b, 3)] for a, b in self.spurious_outside],
        }

    def summary(self) -> str:
        lines = [
            f"gold {self.gold_seconds:.1f}s in {len(self.segments)} segment(s); "
            f"predicted {self.predicted_seconds:.1f}s",
            f"caught {self.caught_seconds:.1f}s (recall {self.recall:.1%}"
            + (f", {self.coverable_recall:.1%} of the {self.coverable_gold_seconds:.1f}s under transcript cues"
               if self.coverable_gold_seconds is not None else "") + "), "
            f"missed {self.missed_seconds:.1f}s, false cut {self.false_cut_seconds:.1f}s "
            f"(precision {self.precision:.1%}), of which {self.false_cut_outside_tolerance_seconds:.1f}s "
            f"more than {self.edge_tolerance:.0f}s from any gold edge",
            f"combined score {self.combined_score:.1f} (weight {self.false_cut_weight})",
        ]
        for s in self.segments:
            tag = f"{s.gold.start:8.1f}-{s.gold.end:8.1f} {s.gold.category:<16} {s.gold.note[:36]:<36}"
            if s.merged_with:
                lines.append(f"  {tag} merged with {s.merged_with} other")
            elif s.found:
                extra = f"  (as {s.split_into} predictions)" if s.split_into > 1 else ""
                lines.append(f"  {tag} found  start {s.start_delta:+6.1f}s  end {s.end_delta:+6.1f}s{extra}")
            else:
                lines.append(f"  {tag} MISSED (coverage {s.coverage:.0%})")
        for a, b in self.spurious:
            if b - a >= 0.5:
                lines.append(f"  cut from editorial: {a:.1f}-{b:.1f} ({b - a:.1f}s)")
        for a, b in self.spurious_outside:
            lines.append(f"  beyond the {self.edge_tolerance:.0f}s edge tolerance: {a:.1f}-{b:.1f} ({b - a:.1f}s)")
        return "\n".join(lines)


def _overlap(a: Interval, b: Interval) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def evaluate(
    predicted: Iterable[Sequence[float]],
    gold: Sequence[GoldAd],
    *,
    duration: float,
    policy_categories: Optional[Set[str]] = None,
    edge_tolerance: float = 3.0,
    match_overlap: float = 0.5,
    coverable: Optional[Iterable[Sequence[float]]] = None,
) -> AdEvaluation:
    """Score ``predicted`` cut intervals against ``gold`` ads.

    Gold ads whose category is outside ``policy_categories`` count as editorial (cutting
    them is a false cut); ambiguous ads of any category are don't-care regions.
    ``duration`` is the episode length and bounds the editorial region.  ``coverable``
    (typically the transcript's cue spans) enables :attr:`AdEvaluation.coverable_recall`.
    """
    if duration <= 0:
        raise ValueError("duration must be > 0")
    cats = set(policy_categories) if policy_categories is not None else None
    in_policy = [g for g in gold if cats is None or g.category in cats]
    dont_care = normalize_intervals([g.interval for g in gold if g.ambiguous], kind="dont_care")
    truth_ads = [g for g in in_policy if not g.ambiguous]
    truth = normalize_intervals([g.interval for g in truth_ads], allow_overlap=False, kind="gold")

    pred_raw = [tuple(p) for p in predicted]
    pred = difference(normalize_intervals(pred_raw, kind="predicted"), dont_care)
    truth_eff = difference(truth, dont_care)

    caught = total_duration(intersection(pred, truth_eff))
    missed = total_duration(difference(truth_eff, pred))
    spurious = difference(pred, truth_eff)
    false_cut = total_duration(spurious)

    # Tolerance zones sit just outside every gold edge.  When a don't-care span (a
    # hand-off line, a music sting) touches the ad, the model's edge naturally lands on
    # the far side of that span, so the zone is measured from there as well.
    zones: List[Interval] = []
    for a, b in truth_eff:
        zones.append((max(0.0, a - edge_tolerance), a))
        zones.append((b, min(duration, b + edge_tolerance)))
    if dont_care:
        # Bridge pauses of up to one tolerance between an ad and its framing line, so a
        # 0.8 s breath between "a word from our sponsors" and the read does not split them.
        pieces = sorted(list(truth_eff) + list(dont_care))
        blocks: List[Interval] = []
        for a, b in pieces:
            if blocks and a <= blocks[-1][1] + edge_tolerance:
                if a > blocks[-1][1]:
                    zones.append((blocks[-1][1], a))  # the bridged pause itself is tolerated
                blocks[-1] = (blocks[-1][0], max(blocks[-1][1], b))
            else:
                blocks.append((a, b))
        for a, b in blocks:
            if any(ta < b and tb > a for ta, tb in truth_eff):
                zones.append((max(0.0, a - edge_tolerance), a))
                zones.append((b, min(duration, b + edge_tolerance)))
    zones_n = normalize_intervals([z for z in zones if z[1] > z[0]], kind="tolerance") if zones else []
    spurious_outside = difference(spurious, zones_n) if zones_n else list(spurious)
    false_cut_outside = total_duration(spurious_outside)

    # per-segment matching against the *raw* predictions: adjacent ads are common in a
    # stacked break, and matching against the merged timeline would fuse two correct
    # segments into one interval with nonsense edges.
    claims: dict = {}
    matches: List[SegmentMatch] = []
    for g in truth_ads:
        # A prediction belongs to a gold segment when the overlap is a substantial part of
        # either: at least half of the prediction (a spot inside a stacked break) or half
        # of the gold segment.  A few seconds of spill-over from the neighbouring ad's
        # prediction is boundary slop, not a second prediction of this segment.
        overlapping = [
            cand for cand in pred_raw
            if _overlap(g.interval, cand) > 0 and (
                _overlap(g.interval, cand) >= match_overlap * (cand[1] - cand[0])
                or _overlap(g.interval, cand) >= match_overlap * g.duration
            )
        ]
        covered = total_duration(intersection(normalize_intervals(overlapping, kind="predicted"), [g.interval])) if overlapping else 0.0
        coverage = covered / g.duration if g.duration > 0 else 0.0
        if overlapping and coverage >= match_overlap:
            # the prediction with the largest overlap "claims" the gold segment; a
            # prediction claiming two gold segments has merged them
            best = max(overlapping, key=lambda cand: _overlap(g.interval, cand))
            claims[best] = claims.get(best, 0) + 1
            matches.append(SegmentMatch(
                g, True, coverage,
                start_delta=min(c[0] for c in overlapping) - g.start,
                end_delta=max(c[1] for c in overlapping) - g.end,
                split_into=len(overlapping),
            ))
        else:
            matches.append(SegmentMatch(g, False, coverage))
    for m in matches:
        if m.found:
            cand = None
            for c in pred_raw:
                if _overlap(m.gold.interval, c) > 0 and claims.get(c, 0) > 1:
                    cand = c
            if cand is not None:
                m.merged_with = claims[cand] - 1

    coverable_gold = caught_coverable = None
    if coverable is not None:
        cover = normalize_intervals(coverable, kind="coverable")
        truth_cov = intersection(truth_eff, cover)
        coverable_gold = total_duration(truth_cov)
        caught_coverable = total_duration(intersection(pred, truth_cov))

    return AdEvaluation(
        coverable_gold_seconds=coverable_gold,
        caught_coverable_seconds=caught_coverable,
        gold_seconds=total_duration(truth_eff),
        predicted_seconds=total_duration(pred),
        caught_seconds=caught,
        missed_seconds=missed,
        false_cut_seconds=false_cut,
        false_cut_outside_tolerance_seconds=false_cut_outside,
        dont_care_seconds=total_duration(dont_care),
        edge_tolerance=edge_tolerance,
        segments=matches,
        spurious=spurious,
        spurious_outside=spurious_outside,
    )
