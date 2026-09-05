"""Step 1 tests for `podcleaner.eval.scoring`.

Covers verification-contract criteria S1.1 (hand-computed cases), S1.2 (normalisation
properties, via hypothesis), S1.3 (explicit asymmetry) and S1.5 (negative controls).

No network, no fixtures, no I/O.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from podcleaner.eval import scoring
from podcleaner.eval.scoring import (
    FALSE_CUT_WEIGHT,
    IntervalError,
    intervals_from_segments,
    intervals_from_transcript,
    normalize_intervals,
    score,
    total_duration,
)
from podcleaner.models import Segment, Transcript

# ======================================================================================
# S1.1 -- hand-computed scorer cases, exact
# ======================================================================================

GOLD = [[10, 20]]

S1_1_CASES = [
    # (predicted,        expected_missed, expected_false_cut)
    ([[10, 20]], 0.0, 0.0),
    ([[10, 30]], 0.0, 10.0),
    ([], 10.0, 0.0),
    ([[0, 30]], 0.0, 20.0),
    ([[12, 18]], 4.0, 0.0),
]


@pytest.mark.parametrize("predicted,expected_missed,expected_false_cut", S1_1_CASES)
def test_s1_1_hand_computed_cases_exact(predicted, expected_missed, expected_false_cut):
    """S1.1: the five cases written out in the contract, to the exact second."""
    result = score(predicted, GOLD)
    assert result.missed_ad_seconds == expected_missed
    assert result.false_cut_seconds == expected_false_cut


def test_s1_1_perfect_prediction_scores_zero():
    """S1.1 corollary: an exact match must be the unique zero of the combined score."""
    assert score([[10, 20]], GOLD).combined_score == 0.0


def test_s1_1_combined_scores_are_hand_computable():
    """S1.1: combined = missed + FALSE_CUT_WEIGHT * false_cut, for each contract case."""
    for predicted, missed, false_cut in S1_1_CASES:
        result = score(predicted, GOLD)
        assert result.combined_score == pytest.approx(
            missed + FALSE_CUT_WEIGHT * false_cut
        ), predicted


def test_s1_1_split_prediction_around_the_ad():
    """Two partial cuts inside one ad: 10-12 and 16-20 -> missed 4 (12..16), no false cut."""
    result = score([[10, 12], [16, 20]], GOLD)
    assert result.missed_ad_seconds == 4.0
    assert result.false_cut_seconds == 0.0
    assert result.caught_ad_seconds == 6.0


def test_s1_1_disjoint_prediction_is_all_wrong_both_ways():
    """A cut that misses the ad entirely: 10 s missed AND 5 s false cut."""
    result = score([[40, 45]], GOLD)
    assert result.missed_ad_seconds == 10.0
    assert result.false_cut_seconds == 5.0
    assert result.caught_ad_seconds == 0.0


def test_s1_1_no_gold_and_no_prediction_is_a_perfect_zero():
    """An episode with no ads, where we cut nothing, is perfect -- not undefined."""
    result = score([], [])
    assert result.missed_ad_seconds == 0.0
    assert result.false_cut_seconds == 0.0
    assert result.combined_score == 0.0
    assert result.recall == 1.0
    assert result.precision == 1.0


def test_s1_1_multi_ad_gold():
    """Hand-computed with two gold ads: gold [[0,10],[20,30]], pred [[5,25]]."""
    result = score([[5, 25]], [[0, 10], [20, 30]])
    # caught 5..10 (5s) and 20..25 (5s) = 10s; missed 0..5 (5s) and 25..30 (5s) = 10s;
    # false cut 10..20 = 10s
    assert result.caught_ad_seconds == 10.0
    assert result.missed_ad_seconds == 10.0
    assert result.false_cut_seconds == 10.0


# ======================================================================================
# S1.2 -- interval normalisation as a property (hypothesis)
# ======================================================================================

_start = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
_length = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False)

interval_strategy = st.builds(lambda s, ln: (s, s + ln), _start, _length)
interval_list_strategy = st.lists(interval_strategy, min_size=0, max_size=25)


@given(intervals=interval_list_strategy)
@settings(max_examples=300, deadline=None)
def test_s1_2_merge_is_idempotent(intervals):
    """S1.2: normalising an already-normalised list changes nothing."""
    once = normalize_intervals(intervals)
    twice = normalize_intervals(once)
    assert once == twice


@given(intervals=interval_list_strategy)
@settings(max_examples=300, deadline=None)
def test_s1_2_merged_total_is_never_greater_than_raw_total(intervals):
    """S1.2: merging can only remove double-counted time, never invent it."""
    raw_total = sum(end - start for start, end in intervals)
    merged_total = total_duration(normalize_intervals(intervals))
    assert merged_total <= raw_total + 1e-9


@given(intervals=interval_list_strategy)
@settings(max_examples=300, deadline=None)
def test_s1_2_merged_intervals_are_sorted_and_disjoint(intervals):
    """S1.2: output is sorted, and no two outputs touch or overlap."""
    merged = normalize_intervals(intervals)
    starts = [start for start, _ in merged]
    assert starts == sorted(starts)
    for (_, prev_end), (next_start, _) in zip(merged, merged[1:]):
        # strictly greater: touching intervals would have been merged
        assert next_start > prev_end


@given(intervals=interval_list_strategy)
@settings(max_examples=300, deadline=None)
def test_s1_2_merge_preserves_coverage(intervals):
    """S1.2: every input interval is fully inside some output interval."""
    merged = normalize_intervals(intervals)
    for start, end in intervals:
        assert any(m_start <= start and end <= m_end for m_start, m_end in merged)


def test_s1_2_touching_intervals_merge():
    """S1.2, named case: [0,5] and [5,9] have no gap, so they are one interval [0,9].

    This is the test that mutation (c) -- changing the merge boundary from `>=` to `>` --
    must break.
    """
    assert normalize_intervals([[0, 5], [5, 9]]) == [(0.0, 9.0)]


def test_s1_2_touching_chain_merges_to_one_interval():
    """A chain of touching intervals collapses to a single span (mutation (c) again)."""
    chain = [[i, i + 1] for i in range(10)]
    assert normalize_intervals(chain) == [(0.0, 10.0)]


def test_s1_2_touching_intervals_merge_after_shuffling():
    """Order must not matter: [5,9] then [0,5] still merges to [0,9]."""
    assert normalize_intervals([[5, 9], [0, 5]]) == [(0.0, 9.0)]


def test_s1_2_touching_gold_does_not_count_as_overlap():
    """Touching is not overlapping: gold [[0,5],[5,9]] is legal and merges."""
    result = score([[0, 9]], [[0, 5], [5, 9]])
    assert result.gold_ad_seconds == 9.0
    assert result.false_cut_seconds == 0.0
    assert result.missed_ad_seconds == 0.0


def test_s1_2_a_one_sample_gap_is_preserved():
    """Negative control for merging: a real gap must NOT be merged away."""
    assert normalize_intervals([[0, 5], [5.001, 9]]) == [(0.0, 5.0), (5.001, 9.0)]


def test_s1_2_overlapping_predictions_merge_rather_than_double_count():
    """[[0,10],[5,20]] covers 20 s, not 25 s."""
    assert normalize_intervals([[0, 10], [5, 20]]) == [(0.0, 20.0)]
    assert score([[0, 10], [5, 20]], []).false_cut_seconds == 20.0


def test_s1_2_output_is_sorted_for_unsorted_input():
    assert normalize_intervals([[30, 40], [0, 5], [10, 12]]) == [
        (0.0, 5.0),
        (10.0, 12.0),
        (30.0, 40.0),
    ]


# ======================================================================================
# S1.3 -- asymmetry is explicit and tested
# ======================================================================================


def test_s1_3_weight_is_a_named_constant_greater_than_one():
    """S1.3: a weight of 1 is not an asymmetric scorer at all.

    Mutation (b) (`FALSE_CUT_WEIGHT = 1`) fails here.
    """
    assert isinstance(FALSE_CUT_WEIGHT, (int, float))
    assert FALSE_CUT_WEIGHT > 1.0


def test_s1_3_one_second_of_false_cut_costs_exactly_weight_times_one_second_of_miss():
    """S1.3, the headline: cost(1 s false cut) == FALSE_CUT_WEIGHT * cost(1 s miss)."""
    one_second_missed = score([], [[0, 1]])
    one_second_false_cut = score([[0, 1]], [])

    assert one_second_missed.missed_ad_seconds == 1.0
    assert one_second_missed.false_cut_seconds == 0.0
    assert one_second_false_cut.missed_ad_seconds == 0.0
    assert one_second_false_cut.false_cut_seconds == 1.0

    assert one_second_missed.combined_score == pytest.approx(1.0)
    assert one_second_false_cut.combined_score == pytest.approx(FALSE_CUT_WEIGHT)
    assert one_second_false_cut.combined_score == pytest.approx(
        FALSE_CUT_WEIGHT * one_second_missed.combined_score
    )
    # And, independent of the constant's value: a false cut must simply hurt more.
    # This is what makes the test fail if the weight is removed entirely.
    assert one_second_false_cut.combined_score > one_second_missed.combined_score


def test_s1_3_score_formula_terms_are_not_swapped():
    """S1.3 / mutation (a): asymmetric error amounts pin down which term is weighted.

    gold [[10,20]], pred [[18,30]] -> missed 8 s, false cut 10 s.
    Correct:  8 + 10*W.   Swapped: 10 + 8*W.  These differ for any W != 1.
    """
    result = score([[18, 30]], [[10, 20]])
    assert result.missed_ad_seconds == pytest.approx(8.0)
    assert result.false_cut_seconds == pytest.approx(10.0)

    correct = 8.0 + 10.0 * FALSE_CUT_WEIGHT
    swapped = 10.0 + 8.0 * FALSE_CUT_WEIGHT
    assert result.combined_score == pytest.approx(correct)
    assert result.combined_score != pytest.approx(swapped)


def test_s1_3_cutting_a_whole_clean_episode_is_worse_than_cutting_nothing():
    """The policy this weight encodes: destroying content beats leaving ads in."""
    gold = [[100, 160]]  # a 60 s ad in a 1 h episode
    cut_everything = score([[0, 3600]], gold)
    cut_nothing = score([], gold)
    assert cut_everything.missed_ad_seconds == 0.0  # it did remove the ad
    assert cut_nothing.false_cut_seconds == 0.0  # it removed no content
    assert cut_everything.combined_score > cut_nothing.combined_score


def test_s1_3_weight_is_recorded_on_the_result():
    assert score([], []).false_cut_weight == FALSE_CUT_WEIGHT
    assert score([[0, 1]], [], false_cut_weight=7.0).false_cut_weight == 7.0
    assert score([[0, 1]], [], false_cut_weight=7.0).combined_score == pytest.approx(7.0)


# ======================================================================================
# S1.5 -- negative controls: invalid input raises, never silently scores
# ======================================================================================

INVALID_INTERVAL_LISTS = [
    pytest.param([[20, 10]], id="end-before-start"),
    pytest.param([[0, 5], [30, 20]], id="end-before-start-second-item"),
    pytest.param([[float("nan"), 10]], id="nan-start"),
    pytest.param([[0, float("nan")]], id="nan-end"),
    pytest.param([[0, float("inf")]], id="inf-end"),
    pytest.param([[float("-inf"), 0]], id="neg-inf-start"),
    pytest.param([["0", "10"]], id="numeric-strings"),
    pytest.param([["a", "b"]], id="non-numeric-strings"),
    pytest.param([[None, 10]], id="none-start"),
    pytest.param([[True, False]], id="bools"),
    pytest.param([[0, 10, 20]], id="three-elements"),
    pytest.param([[5]], id="one-element"),
    pytest.param([[]], id="empty-pair"),
    pytest.param([5], id="bare-number-not-a-pair"),
    pytest.param(["0,10"], id="string-item"),
    pytest.param([{"start": 0, "end": 10}], id="dict-item"),
    pytest.param([[-5, 10]], id="negative-start"),
    pytest.param("0,10", id="whole-input-is-a-string"),
    pytest.param(17, id="whole-input-not-iterable"),
]


@pytest.mark.parametrize("bad", INVALID_INTERVAL_LISTS)
def test_s1_5_invalid_predicted_raises(bad):
    """S1.5: bad predicted input raises rather than scoring something plausible."""
    with pytest.raises(IntervalError):
        score(bad, [[0, 10]])


@pytest.mark.parametrize("bad", INVALID_INTERVAL_LISTS)
def test_s1_5_invalid_gold_raises(bad):
    """S1.5: bad gold input raises rather than scoring something plausible."""
    with pytest.raises(IntervalError):
        score([[0, 10]], bad)


@pytest.mark.parametrize("bad", INVALID_INTERVAL_LISTS)
def test_s1_5_normalize_rejects_the_same_inputs(bad):
    with pytest.raises(IntervalError):
        normalize_intervals(bad)


@pytest.mark.parametrize(
    "overlapping_gold",
    [
        pytest.param([[0, 10], [5, 20]], id="partial-overlap"),
        pytest.param([[0, 100], [10, 20]], id="containment"),
        pytest.param([[10, 20], [0, 15]], id="unsorted-overlap"),
        pytest.param([[0, 10], [0, 10]], id="exact-duplicate"),
    ],
)
def test_s1_5_overlapping_gold_raises(overlapping_gold):
    """S1.5: overlapping GOLD is a broken ground truth and must never be scored against."""
    with pytest.raises(IntervalError, match="overlapping"):
        score([[0, 5]], overlapping_gold)


@pytest.mark.parametrize(
    "overlapping_predicted",
    [[[0, 10], [5, 20]], [[0, 100], [10, 20]], [[0, 10], [0, 10]]],
)
def test_s1_5_overlapping_predicted_is_merged_not_rejected(overlapping_predicted):
    """The asymmetry in validation: a sloppy detector is merged, broken gold is refused."""
    result = score(overlapping_predicted, [])
    assert result.false_cut_seconds > 0


def test_s1_5_interval_error_is_a_value_error():
    """So existing `except ValueError` call sites keep working."""
    assert issubclass(IntervalError, ValueError)


@pytest.mark.parametrize("bad_weight", [float("nan"), -1.0, "3", None, True])
def test_s1_5_invalid_weight_raises(bad_weight):
    with pytest.raises(IntervalError):
        score([[0, 1]], [[0, 1]], false_cut_weight=bad_weight)


def test_s1_5_error_messages_name_which_side_was_bad():
    """A scorer that raises unhelpfully is only half useful."""
    with pytest.raises(IntervalError, match="gold"):
        score([[0, 1]], [[10, 5]])
    with pytest.raises(IntervalError, match="predicted"):
        score([[10, 5]], [[0, 1]])


# ======================================================================================
# bridges to the existing domain models
# ======================================================================================


def test_intervals_from_segments_uses_only_ad_segments():
    segments = [
        Segment(id=0, text="hello", start=0.0, end=10.0, is_ad=False),
        Segment(id=1, text="buy", start=10.0, end=15.0, is_ad=True),
        Segment(id=2, text="now", start=15.0, end=20.0, is_ad=True),
        Segment(id=3, text="anyway", start=20.0, end=30.0, is_ad=False),
    ]
    # segments 1 and 2 touch, so they merge into one ad break
    assert intervals_from_segments(segments) == [(10.0, 20.0)]


def test_intervals_from_transcript_round_trips_into_the_scorer():
    transcript = Transcript(
        segments=[
            Segment(id=0, text="a", start=0.0, end=10.0, is_ad=False),
            Segment(id=1, text="b", start=10.0, end=20.0, is_ad=True),
        ]
    )
    predicted = intervals_from_transcript(transcript)
    assert score(predicted, [[10, 20]]).combined_score == 0.0


def test_no_ad_segments_yields_no_intervals():
    transcript = Transcript(
        segments=[Segment(id=0, text="a", start=0.0, end=10.0, is_ad=False)]
    )
    assert intervals_from_transcript(transcript) == []


# ======================================================================================
# assorted invariants
# ======================================================================================


@given(
    predicted=interval_list_strategy,
    gold_start=_start,
    gold_len=st.floats(min_value=0.1, max_value=500.0),
)
@settings(max_examples=200, deadline=None)
def test_caught_plus_missed_equals_gold_total(predicted, gold_start, gold_len):
    """Whatever the prediction, every gold second is either caught or missed."""
    gold = [[gold_start, gold_start + gold_len]]
    result = score(predicted, gold)
    assert result.caught_ad_seconds + result.missed_ad_seconds == pytest.approx(
        result.gold_ad_seconds, abs=1e-6
    )


@given(predicted=interval_list_strategy, gold=interval_list_strategy)
@settings(max_examples=200, deadline=None)
def test_caught_plus_false_cut_equals_predicted_total(predicted, gold):
    """Every predicted second is either a correct cut or a false cut."""
    gold_norm = normalize_intervals(gold)
    result = score(predicted, gold_norm)
    assert result.caught_ad_seconds + result.false_cut_seconds == pytest.approx(
        result.predicted_cut_seconds, abs=1e-6
    )


@given(predicted=interval_list_strategy, gold=interval_list_strategy)
@settings(max_examples=200, deadline=None)
def test_combined_score_is_non_negative_and_finite(predicted, gold):
    result = score(predicted, normalize_intervals(gold))
    assert result.combined_score >= 0.0
    assert math.isfinite(result.combined_score)


def test_score_result_to_dict_is_json_shaped():
    payload = score([[0, 5]], [[0, 10]]).to_dict()
    assert payload["missed_ad_seconds"] == 5.0
    assert payload["false_cut_seconds"] == 0.0
    assert payload["false_cut_weight"] == FALSE_CUT_WEIGHT
    assert set(payload) == {
        "missed_ad_seconds",
        "false_cut_seconds",
        "caught_ad_seconds",
        "gold_ad_seconds",
        "predicted_cut_seconds",
        "combined_score",
        "false_cut_weight",
        "recall",
        "precision",
    }


def test_module_exposes_the_weight_by_that_exact_name():
    """The contract names the constant; keep the name stable."""
    assert hasattr(scoring, "FALSE_CUT_WEIGHT")
