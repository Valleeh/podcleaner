"""Hand-computed cases for podcleaner.eval.adscore and the ported scorer."""

from __future__ import annotations

import pytest

from podcleaner.eval.adscore import GoldAd, evaluate
from podcleaner.eval.scoring import FALSE_CUT_WEIGHT, IntervalError, normalize_intervals, score


def test_scorer_contract_cases():
    gold = [[10, 20]]
    assert (score([[10, 20]], gold).missed_ad_seconds, score([[10, 20]], gold).false_cut_seconds) == (0, 0)
    assert score([[10, 30]], gold).false_cut_seconds == 10
    assert score([], gold).missed_ad_seconds == 10
    assert score([[0, 30]], gold).false_cut_seconds == 20
    assert score([[12, 18]], gold).missed_ad_seconds == 4


def test_scorer_asymmetry_is_the_weight():
    r = score([[10, 21]], [[10, 20]])  # 1 s false cut
    m = score([[10, 19]], [[10, 20]])  # 1 s missed
    assert r.combined_score == pytest.approx(FALSE_CUT_WEIGHT * m.combined_score)
    assert FALSE_CUT_WEIGHT > 1


def test_scorer_refuses_invalid_input():
    for bad in ([[5, 2]], [[float("nan"), 3]], [["a", 3]], [[0, 5], [3, 8]]):
        with pytest.raises(IntervalError):
            score([], bad)


def test_touching_intervals_merge():
    assert normalize_intervals([[0, 5], [5, 9]]) == [(0.0, 9.0)]


GOLD = [
    GoldAd(100, 200, "sponsor_read", note="A"),
    GoldAd(200, 260, "cross_promo", note="B"),
    GoldAd(500, 520, "self_promo", ambiguous=True, note="plug"),
    GoldAd(800, 830, "self_promo", note="outro"),
]
POLICY = {"sponsor_read", "host_endorsement", "cross_promo"}


def test_perfect_prediction():
    e = evaluate([(100, 200), (200, 260)], GOLD, duration=1000, policy_categories=POLICY)
    assert e.recall == 1.0 and e.precision == 1.0 and e.false_cut_seconds == 0
    assert e.combined_score == 0
    # out-of-policy segments are editorial, not gold, so they do not appear per-segment
    assert [s.found for s in e.segments] == [True, True]


def test_nothing_predicted_is_safe_but_misses_everything():
    e = evaluate([], GOLD, duration=1000, policy_categories=POLICY)
    assert e.recall == 0 and e.precision == 1.0 and e.false_cut_seconds == 0
    assert e.missed_seconds == 160


def test_dont_care_region_neither_rewards_nor_punishes():
    e = evaluate([(100, 200), (200, 260), (505, 515)], GOLD, duration=1000, policy_categories=POLICY)
    assert e.false_cut_seconds == 0 and e.caught_seconds == 160
    assert e.dont_care_seconds == 20


def test_edge_tolerance_separates_slop_from_content_destruction():
    e = evaluate([(98, 262), (900, 910)], GOLD, duration=1000, policy_categories=POLICY, edge_tolerance=3.0)
    assert e.false_cut_seconds == pytest.approx(14)
    assert e.false_cut_outside_tolerance_seconds == pytest.approx(10)
    assert e.spurious == [(98.0, 100.0), (260.0, 262.0), (900.0, 910.0)]
    assert e.spurious_outside == [(900.0, 910.0)]
    assert "beyond the 3s edge tolerance: 900.0-910.0" in e.summary()


def test_cutting_out_of_policy_self_promo_is_a_false_cut():
    e = evaluate([(800, 830)], GOLD, duration=1000, policy_categories=POLICY)
    assert e.false_cut_seconds == 30 and e.false_cut_outside_tolerance_seconds == 30


def test_per_segment_deltas_and_merge_detection():
    e = evaluate([(97, 205), (203, 258)], GOLD, duration=1000, policy_categories=POLICY)
    a, b = e.segments[0], e.segments[1]
    assert a.found and a.start_delta == -3 and a.end_delta == 5
    assert b.found and b.start_delta == 3 and b.end_delta == -2
    m = evaluate([(100, 260)], GOLD, duration=1000, policy_categories=POLICY)
    assert m.segments[0].merged_with == 1 and m.segments[1].merged_with == 1


def test_invalid_duration_and_gold_rejected():
    with pytest.raises(ValueError):
        evaluate([], GOLD, duration=0)
    with pytest.raises(IntervalError):
        evaluate([], [GoldAd(10, 20), GoldAd(15, 25)], duration=100)


def test_coverable_recall_ignores_uncued_tail_of_an_ad():
    """An inserted spot runs 100-140 but the last transcript cue ends at 134: a cue-aligned
    answer 100-134 is as good as it gets, and coverable recall says so while raw recall
    does not."""
    gold = [GoldAd(100, 140, "sponsor_read")]
    cues = [(90, 100), (100, 120), (120, 134), (140, 150)]
    e = evaluate([(100, 134)], gold, duration=1000, coverable=cues)
    assert e.recall == pytest.approx(34 / 40)
    assert e.coverable_gold_seconds == pytest.approx(34)
    assert e.coverable_recall == pytest.approx(1.0)
    assert e.to_dict()["coverable_recall"] == 1.0
    plain = evaluate([(100, 134)], gold, duration=1000)
    assert plain.coverable_recall == plain.recall


def test_stacked_break_reported_as_two_predictions_counts_as_found():
    """One server insert carrying two spots is one gold region; a model that correctly
    reports the two spots separately has still found the region."""
    gold = [GoldAd(0, 56, "sponsor_read", note="pre-roll with two spots")]
    e = evaluate([(0, 28), (28, 56)], gold, duration=1000)
    [m] = e.segments
    assert m.found and m.coverage == pytest.approx(1.0) and m.split_into == 2
    assert m.start_delta == 0 and m.end_delta == 0 and m.merged_with == 0
    half = evaluate([(0, 27)], gold, duration=1000)
    assert half.segments[0].found is False and half.segments[0].coverage == pytest.approx(27 / 56)


def test_tolerance_extends_across_an_adjacent_dont_care_span():
    """Gold ad 100-190 with an ambiguous hand-off line 95-100 in front of it.  A cut that
    starts at 93 is 2 s outside the hand-off, i.e. within tolerance of the block's edge,
    not 7 s outside the ad."""
    gold = [GoldAd(100, 190, "sponsor_read"), GoldAd(95, 100, "other", ambiguous=True)]
    e = evaluate([(93, 190)], gold, duration=1000)
    assert e.false_cut_seconds == pytest.approx(2)
    assert e.false_cut_outside_tolerance_seconds == 0
    far = evaluate([(90, 190)], gold, duration=1000)
    assert far.false_cut_outside_tolerance_seconds == pytest.approx(2)  # 90-92 is beyond 95-3


def test_tolerance_bridges_a_short_pause_between_framing_line_and_ad():
    gold = [GoldAd(100, 190, "sponsor_read"), GoldAd(94, 99.2, "other", ambiguous=True)]  # 0.8 s breath at 99.2-100
    e = evaluate([(93.5, 190)], gold, duration=1000)
    assert e.false_cut_outside_tolerance_seconds == 0
    far = evaluate([(85, 190)], gold, duration=1000)
    assert far.false_cut_outside_tolerance_seconds == pytest.approx(6)  # 85-91 is beyond 94-3


def test_pause_between_two_framing_spans_inside_a_block_is_tolerated():
    """Hand-off line (ambiguous), 0.5 s breath, ambiguous own-product plug, ad.  A cut across
    the whole block destroys no content."""
    gold = [GoldAd(100, 101.9, "other", ambiguous=True), GoldAd(102.4, 160, "self_promo", ambiguous=True),
            GoldAd(160.7, 240, "sponsor_read")]
    e = evaluate([(100, 240)], gold, duration=1000)
    assert e.false_cut_outside_tolerance_seconds == 0
    assert e.false_cut_seconds == pytest.approx(0.5 + 0.7)
