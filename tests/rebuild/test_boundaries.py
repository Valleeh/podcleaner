"""Step 5: boundary snapping, the refusal rule, cutting, chapter shifting.

Criteria S5.1 - S5.6 of ``docs/verification-contract.md``.

Three things make these tests evidence rather than decoration:

* **Ground truth by construction.**  Every piece of audio here is built by splicing
  parts of known length, so the silence really is at the second we say it is.  Nobody
  judged anything.
* **Negative controls.**  ``test_s5_2_*`` is the whole policy: audio with no clean edge
  must be left alone.  A snapper that always cuts passes a positive-only suite.
* **An independent oracle.**  Output durations are read back with **ffprobe**
  (``podcleaner.eval.corpus.ffprobe_*``, which shell out to the binary), never with the
  cutter's own arithmetic.  Checking our cut maths with our cut maths would be circular.

Everything is offline: ffmpeg's ``lavfi`` synthesises every sample.
"""

from __future__ import annotations

import math
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from podcleaner.detect.boundaries import (  # noqa: E402
    DEFAULT_EDGE_GUARD_SECONDS,
    BoundaryError,
    Chapter,
    RefusalReason,
    Silence,
    SilenceMap,
    SnapOutcome,
    Word,
    apply_cuts,
    detect_silences,
    plan_cuts,
    probe_audio,
    shift_chapters,
    snap,
    snap_segment,
    word_at,
)
from podcleaner.eval.corpus import (  # noqa: E402
    ffprobe_duration_seconds,
    ffprobe_sample_count,
    gold_intervals,
    load_manifest,
)
from podcleaner.eval.scoring import FALSE_CUT_WEIGHT, score  # noqa: E402

SAMPLE_RATE = 16_000
FRAME = 1.0 / SAMPLE_RATE

CORPUS_DIR = REPO_ROOT / "corpus" / "synthetic"

#: lavfi graphs.  ``{sr}`` and ``{dur}`` are filled in by :func:`_synth`.
TONE = "sine=frequency={freq}:sample_rate={{sr}}:duration={{dur}}"
#: "Silence" is low-level room tone at about -66 dBFS, not digital zero -- a real pause
#: in a real recording is never bit-exact zero, and silencedetect must cope with that.
ROOM_TONE = "anoisesrc=color=pink:sample_rate={{sr}}:duration={{dur}}:seed={seed}:amplitude=0.0005"


# --------------------------------------------------------------------------------------
# audio construction helpers (local, so the tests own their ground truth)
# --------------------------------------------------------------------------------------


def _ffmpeg(args: Sequence[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()}")


def _synth(graph: str, seconds: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        [
            "-f", "lavfi",
            "-i", graph.format(sr=SAMPLE_RATE, dur=f"{seconds:.6f}"),
            "-ac", "1",
            "-ar", str(SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-t", f"{seconds:.6f}",
            "-fflags", "+bitexact",
            "-flags", "+bitexact",
            "-map_metadata", "-1",
            str(dest),
        ]
    )


def _concat(parts: Sequence[Path], dest: Path) -> None:
    listing = dest.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts))
    _ffmpeg(
        [
            "-f", "concat",
            "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            "-fflags", "+bitexact",
            "-flags", "+bitexact",
            "-map_metadata", "-1",
            str(dest),
        ]
    )
    listing.unlink()


@dataclass
class BuiltAudio:
    """An audio file plus the timeline we built it from -- the ground truth."""

    path: Path
    duration: float
    silences: List[Tuple[float, float]]
    ads: List[Tuple[float, float]]
    words: List[Word]


def _build(spec: Sequence[Tuple[str, float, str]], dest: Path) -> BuiltAudio:
    """Render ``spec`` -- a list of ``(kind, seconds, graph)`` -- as one WAV.

    ``kind`` is ``"content"``, ``"ad"``, ``"gap"`` or ``"word"``; it is what turns the
    plan into ground truth.  Durations are whole tenths/hundredths of a second, which are
    exact in samples at 16 kHz, so nothing drifts.
    """
    parts: List[Path] = []
    silences: List[Tuple[float, float]] = []
    ads: List[Tuple[float, float]] = []
    words: List[Word] = []
    cursor = 0.0
    work = dest.parent / f".{dest.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)
    for index, (kind, seconds, graph) in enumerate(spec):
        chunk = work / f"{index:03d}_{kind}.wav"
        _synth(graph, seconds, chunk)
        parts.append(chunk)
        start, end = cursor, cursor + seconds
        if kind == "gap":
            silences.append((start, end))
        elif kind == "ad":
            ads.append((start, end))
        elif kind == "word":
            words.append(Word(start, end, f"w{index}"))
        cursor = end
    _concat(parts, dest)
    return BuiltAudio(
        path=dest, duration=cursor, silences=silences, ads=ads, words=words
    )


# --------------------------------------------------------------------------------------
# fixtures (module-local; tests/rebuild/conftest.py belongs to other steps)
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workdir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("boundaries")


@pytest.fixture(scope="module")
def marked_episode(workdir: Path) -> BuiltAudio:
    """22.0 s: content, pause, ad, pause, content.  Pauses at known instants.

    Timeline, exactly::

        0.00 -  8.00  content
        8.00 -  8.40  pause      <- silence centred on 8.20
        8.40 - 14.40  ad
       14.40 - 14.80  pause      <- silence centred on 14.60
       14.80 - 22.00  content
    """
    spec = [
        ("content", 8.0, TONE.format(freq=180)),
        ("gap", 0.4, ROOM_TONE.format(seed=11)),
        ("ad", 6.0, TONE.format(freq=900)),
        ("gap", 0.4, ROOM_TONE.format(seed=12)),
        ("content", 7.2, TONE.format(freq=210)),
    ]
    return _build(spec, workdir / "marked.wav")


@pytest.fixture(scope="module")
def unmarked_episode(workdir: Path) -> BuiltAudio:
    """22.0 s with the same ad in the same place but **no pauses at all**.

    This is the S5.2 negative control: the boundaries are exactly as real as in
    ``marked_episode``, but there is no acoustic edge to snap to.  The correct behaviour
    is to keep the segment.
    """
    spec = [
        ("content", 8.0, TONE.format(freq=180)),
        ("ad", 6.4, TONE.format(freq=900)),
        ("content", 7.6, TONE.format(freq=210)),
    ]
    return _build(spec, workdir / "unmarked.wav")


@pytest.fixture(scope="module")
def sentence_episode(workdir: Path) -> BuiltAudio:
    """Four "sentences" of five 0.5 s "words".

    Intra-sentence gaps are 0.08 s -- shorter than ``min_silence_seconds``, so they are
    *not* offered as snap targets (a breath between words is not a boundary).  Between
    sentences there is a 0.45 s pause, which is.  ``words`` is the exact word-timestamp
    fixture S5.3 asks for.
    """
    spec: List[Tuple[str, float, str]] = []
    for sentence in range(4):
        for word in range(5):
            spec.append(("word", 0.5, TONE.format(freq=150 + 40 * word)))
            if word < 4:
                spec.append(("hush", 0.08, ROOM_TONE.format(seed=20 + word)))
        spec.append(("gap", 0.45, ROOM_TONE.format(seed=30 + sentence)))
    return _build(spec, workdir / "sentences.wav")


@pytest.fixture(scope="module")
def step1_manifest() -> dict:
    if not (CORPUS_DIR / "manifest.json").exists():
        pytest.skip(
            "corpus/synthetic not generated; run "
            "`python -m podcleaner.eval.corpus --out corpus/synthetic`"
        )
    return load_manifest(CORPUS_DIR)


# --------------------------------------------------------------------------------------
# S5.1 -- the snapped boundary lands on the known silence
# --------------------------------------------------------------------------------------


def test_s5_1_detection_finds_the_constructed_silences(marked_episode: BuiltAudio):
    """Before snapping means anything, detection has to find the real pauses."""
    found = detect_silences(marked_episode.path)
    assert len(found) == len(marked_episode.silences) == 2
    for region, (start, end) in zip(found.regions, marked_episode.silences):
        assert region.start == pytest.approx(start, abs=0.01)
        assert region.end == pytest.approx(end, abs=0.01)


@pytest.mark.parametrize("offset", [-0.9, -0.5, -0.21, 0.0, 0.21, 0.5, 0.9])
def test_s5_1_snap_lands_within_tolerance_of_the_known_silence(
    marked_episode: BuiltAudio, offset: float
):
    """S5.1: silence at a known t -> the snapped boundary lands within tolerance of t.

    The pause runs 8.00-8.40, so t = 8.20.  A boundary proposed anywhere within a second
    of it must come back inside that pause -- i.e. within 0.20 s of t.
    """
    t = 8.20
    half_pause = 0.20
    decision = snap(t + offset, marked_episode.path, 1.0)

    assert decision.accepted, decision.log_fields()
    assert abs(decision.snapped - t) <= half_pause + 0.01, decision.log_fields()
    assert decision.silence.start == pytest.approx(8.0, abs=0.01)


@pytest.mark.parametrize(
    "proposed,expected_direction",
    [(8.9, "earlier"), (7.5, "later"), (8.2, "unchanged")],
)
def test_s5_1_snap_moves_toward_the_silence_not_away(
    marked_episode: BuiltAudio, proposed: float, expected_direction: str
):
    """The direction is the point: snapping away from the pause cuts deeper into speech.

    This is the assertion that mutation (b), "snap in the wrong direction", has to break.
    """
    decision = snap(proposed, marked_episode.path, 1.5)
    assert decision.accepted, decision.log_fields()
    assert decision.direction == expected_direction, decision.log_fields()
    assert 8.0 <= decision.snapped <= 8.4


@pytest.mark.parametrize("t", [3.0, 12.5, 40.0])
@pytest.mark.parametrize("approach", [-0.7, 0.7])
def test_s5_1_snap_on_a_constructed_silence_map(t: float, approach: float):
    """Same criterion without ffmpeg in the loop, over several known instants.

    The silence map is supplied directly, so this isolates the snap arithmetic from the
    detector.
    """
    silences = [(t - 0.15, t + 0.15)]
    decision = snap(t + approach, silences, 1.0)
    assert decision.accepted
    assert abs(decision.snapped - t) <= 0.15
    # moved toward the silence, never past it or away from it
    assert abs(decision.snapped - t) < abs((t + approach) - t)
    assert math.copysign(1.0, decision.shift) == math.copysign(1.0, -approach)


def test_s5_1_boundary_already_inside_the_silence_does_not_move():
    decision = snap(10.0, [(9.5, 10.5)], 1.0)
    assert decision.outcome is SnapOutcome.UNCHANGED
    assert decision.snapped == 10.0
    assert decision.shift == 0.0


def test_s5_1_nearest_of_several_silences_wins():
    decision = snap(10.0, [(2.0, 3.0), (9.0, 9.2), (12.0, 13.0)], 5.0)
    assert decision.accepted
    assert decision.silence == Silence(9.0, 9.2)


# --------------------------------------------------------------------------------------
# S5.2 -- THE CORE POLICY.  No clean edge within tolerance -> keep the segment.
# --------------------------------------------------------------------------------------


def test_s5_2_unmarked_episode_really_has_no_detectable_edge(unmarked_episode: BuiltAudio):
    """The negative control is only a control if the audio genuinely has no pause."""
    assert len(detect_silences(unmarked_episode.path)) == 0


def test_s5_2_no_edge_within_tolerance_keeps_the_segment(unmarked_episode: BuiltAudio):
    """S5.2: the ad is really there, at exactly 8.0-14.4, and we still must not cut it.

    A snapper that cuts anyway "gets the ad" -- and clips the sentence on either side of
    it.  The memo ranks that as the worse outcome, so the policy is to keep.
    """
    proposed = [[8.0, 14.4]]
    plan = plan_cuts(proposed, unmarked_episode.path, 1.5)

    assert plan.cuts == []
    assert plan.total_cut_seconds == 0.0
    assert plan.refusal_count == 1

    kept = plan.kept_segments[0]
    assert kept.kept is True
    assert kept.cut is None
    assert kept.reason == RefusalReason.START_REFUSED.value
    assert kept.start_decision.outcome is SnapOutcome.REFUSED
    assert (
        kept.start_decision.reason == RefusalReason.NO_EDGE_WITHIN_TOLERANCE.value
    )
    # the refusal is an explicit, loggable record -- not a silent fallback
    record = kept.log_fields()
    assert record["event"] == "segment_kept"
    assert record["cut_start"] is None
    assert record["reason"] == RefusalReason.START_REFUSED.value


def test_s5_2_refusal_survives_to_the_output_file(
    unmarked_episode: BuiltAudio, workdir: Path
):
    """End to end: a refused segment means the audio comes out the same length.

    Checked with ffprobe, not with the cutter's own bookkeeping.
    """
    plan = plan_cuts([[8.0, 14.4]], unmarked_episode.path, 1.5)
    out = workdir / "refused.wav"
    apply_cuts(unmarked_episode.path, out, plan.cuts)

    assert ffprobe_sample_count(out) == ffprobe_sample_count(unmarked_episode.path)
    assert ffprobe_duration_seconds(out) == pytest.approx(
        ffprobe_duration_seconds(unmarked_episode.path), abs=FRAME
    )


def test_s5_2_edge_just_outside_tolerance_is_refused(marked_episode: BuiltAudio):
    """The pause exists, but too far away to plausibly be *this* boundary's edge."""
    # nearest pause edge is at 8.40; propose 11.0 with a 1.0 s tolerance -> 2.6 s away
    decision = snap(11.0, marked_episode.path, 1.0)
    assert decision.refused
    assert decision.snapped is None
    assert decision.reason == RefusalReason.NO_EDGE_WITHIN_TOLERANCE.value
    # ...and the same boundary IS accepted once the tolerance genuinely reaches it
    assert snap(11.0, marked_episode.path, 3.0).accepted


def test_s5_2_one_bad_edge_keeps_the_whole_segment(marked_episode: BuiltAudio):
    """Half a clean cut is not half as good -- it is a clipped sentence.  Keep it all."""
    # start 8.2 sits in the first pause; end 11.0 sits in the middle of the ad
    decision = snap_segment([8.2, 11.0], marked_episode.path, 1.0)
    assert decision.kept is True
    assert decision.reason == RefusalReason.END_REFUSED.value
    assert decision.start_decision.accepted
    assert decision.end_decision.refused


def test_s5_2_ad_free_audio_yields_no_cuts(marked_episode: BuiltAudio):
    """Nothing proposed -> nothing cut.  (A detector that fires on nothing is step 4's job.)"""
    plan = plan_cuts([], marked_episode.path, 1.5)
    assert plan.cuts == []
    assert plan.segments == ()


# --------------------------------------------------------------------------------------
# S5.3 -- a cut boundary never falls strictly inside a word
# --------------------------------------------------------------------------------------


def test_s5_3_snapped_boundaries_never_land_inside_a_word(sentence_episode: BuiltAudio):
    """S5.3, swept over the whole episode against the word-timestamp fixture."""
    words = sentence_episode.words
    assert len(words) == 20

    silences = detect_silences(sentence_episode.path)
    # only the 0.45 s sentence pauses are edges; the 0.08 s intra-sentence gaps are not
    assert len(silences) == 4, [r.as_tuple() for r in silences]

    accepted = 0
    proposals = [round(0.1 * i, 2) for i in range(1, int(sentence_episode.duration * 10))]
    for proposed in proposals:
        decision = snap(proposed, silences, 1.0, words=words)
        if not decision.accepted:
            continue
        accepted += 1
        offender = word_at(decision.snapped, words)
        assert offender is None, (
            f"boundary proposed at {proposed} snapped to {decision.snapped}, "
            f"strictly inside word {offender}"
        )
    # guard against a vacuous pass: the sweep must actually accept a lot of boundaries
    assert accepted >= 40, f"only {accepted} of {len(proposals)} proposals were accepted"


def test_s5_3_cut_plan_boundaries_avoid_words(sentence_episode: BuiltAudio):
    """The same property at segment level, for the interval a detector would propose."""
    words = sentence_episode.words
    # sentence 2 (words 5-9) proposed sloppily, 0.3 s adrift at each end
    proposed = [[words[5].start - 0.3, words[9].end + 0.3]]
    plan = plan_cuts(proposed, sentence_episode.path, 1.0, words=words)

    assert len(plan.cuts) == 1
    for edge in plan.cuts[0]:
        assert word_at(edge, words) is None, f"cut edge {edge} is inside a word"


def test_s5_3_edge_inside_a_word_is_refused_not_used():
    """If the only candidate edge overlaps a word, refuse -- do not cut there anyway."""
    words = [Word(9.8, 10.6, "mid")]
    decision = snap(10.0, [(9.9, 10.5)], 1.0, words=words)
    assert decision.refused
    assert decision.reason == RefusalReason.EDGE_INSIDE_WORD.value


def test_s5_3_word_at_treats_the_exact_edges_as_outside():
    """Cutting at the instant a word starts or ends removes nothing of the word."""
    words = [Word(1.0, 2.0, "hello")]
    assert word_at(1.0, words) is None
    assert word_at(2.0, words) is None
    assert word_at(1.5, words) is not None


# --------------------------------------------------------------------------------------
# S5.4 -- ffprobe oracle for output duration
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cuts",
    [
        [],
        [(8.0, 14.8)],
        [(0.0, 2.0)],
        [(20.0, 22.0)],
        [(2.0, 4.0), (10.0, 12.0), (18.0, 20.0)],
        [(3.0, 5.0), (5.0, 7.0)],          # touching cuts merge
        [(3.0, 6.0), (4.0, 8.0)],          # overlapping cuts merge
        [(1.0, 1.0 + FRAME)],              # a single frame
    ],
)
def test_s5_4_ffprobe_says_output_is_input_minus_the_cuts(
    marked_episode: BuiltAudio, workdir: Path, cuts
):
    """S5.4: duration_out == duration_in - sum(cuts), within one frame, per ffprobe.

    ``ffprobe_duration_seconds`` shells out to the ffprobe binary and shares no code with
    ``apply_cuts``; the sample-count assertion below is stricter still (PCM sample counts
    are exact, so a one-frame slip is visible).
    """
    name = "cut_" + "_".join(f"{a:g}-{b:g}" for a, b in cuts) if cuts else "cut_none"
    out = workdir / f"{name}.wav"

    result = apply_cuts(marked_episode.path, out, cuts)

    in_duration = ffprobe_duration_seconds(marked_episode.path)
    out_duration = ffprobe_duration_seconds(out)
    removed = sum(b - a for a, b in result.cuts)  # after merging, so no double-counting

    assert out_duration == pytest.approx(in_duration - removed, abs=FRAME)
    assert ffprobe_sample_count(out) == ffprobe_sample_count(
        marked_episode.path
    ) - result.removed_samples


def test_s5_4_end_to_end_plan_then_cut(marked_episode: BuiltAudio, workdir: Path):
    """The realistic path: sloppy detection -> snap -> cut -> ffprobe."""
    plan = plan_cuts([[7.6, 15.1]], marked_episode.path, 1.5)
    assert len(plan.cuts) == 1

    out = workdir / "planned.wav"
    apply_cuts(marked_episode.path, out, plan.cuts)

    expected = ffprobe_duration_seconds(marked_episode.path) - plan.total_cut_seconds
    assert ffprobe_duration_seconds(out) == pytest.approx(expected, abs=FRAME)
    # and the cut really did land in the pauses, not in the speech
    assert 8.0 <= plan.cuts[0][0] <= 8.4
    assert 14.4 <= plan.cuts[0][1] <= 14.8


def test_s5_4_cuts_land_on_frame_boundaries(marked_episode: BuiltAudio, workdir: Path):
    """A cut at a non-integral sample is rounded to a frame, and stays exact after that."""
    odd = 3.0 + 0.5 * FRAME
    result = apply_cuts(marked_episode.path, workdir / "odd.wav", [(odd, odd + 1.0)])
    for lo, hi in result.kept_ranges:
        assert isinstance(lo, int) and isinstance(hi, int)
    assert ffprobe_sample_count(workdir / "odd.wav") == result.expected_output_samples


def test_s5_4_refuses_to_write_an_empty_file(marked_episode: BuiltAudio, workdir: Path):
    """Negative control for the cutter: cutting everything is an error, not a 0-byte file."""
    with pytest.raises(BoundaryError, match="refusing to write an empty file"):
        apply_cuts(marked_episode.path, workdir / "empty.wav", [(0.0, 30.0)])


def test_s5_4_rejects_invalid_cuts(marked_episode: BuiltAudio, workdir: Path):
    with pytest.raises(BoundaryError):
        apply_cuts(marked_episode.path, workdir / "bad.wav", [(5.0, 1.0)])
    with pytest.raises(BoundaryError):
        apply_cuts(marked_episode.path, workdir / "bad.wav", [(float("nan"), 1.0)])
    with pytest.raises(BoundaryError):
        apply_cuts(marked_episode.path, workdir / "bad.wav", [(-1.0, 1.0)])


# --------------------------------------------------------------------------------------
# S5.5 -- chapter marks
# --------------------------------------------------------------------------------------


def test_s5_5_chapter_after_a_cut_moves_back_by_the_cut_duration():
    """S5.5, hand-computable: cut [10, 20] (D = 10) -> 5 stays, 30 -> 20, 60 -> 50."""
    chapters = [Chapter(5.0, "intro"), Chapter(30.0, "topic"), Chapter(60.0, "outro")]
    shifted = shift_chapters(chapters, [(10.0, 20.0)])

    assert [c.start for c in shifted] == [5.0, 20.0, 50.0]
    assert [c.original_start for c in shifted] == [5.0, 30.0, 60.0]
    assert [c.shift for c in shifted] == [0.0, -10.0, -10.0]
    assert [c.title for c in shifted] == ["intro", "topic", "outro"]


def test_s5_5_multiple_cuts_accumulate():
    """Two cuts of 10 s and 5 s before T=100 -> T lands at 85."""
    shifted = shift_chapters([Chapter(100.0, "late")], [(10.0, 20.0), (40.0, 45.0)])
    assert shifted[0].start == 85.0
    assert shifted[0].removed_before == 15.0


def test_s5_5_cut_after_the_chapter_does_not_move_it():
    shifted = shift_chapters([Chapter(10.0, "early")], [(20.0, 30.0)])
    assert shifted[0].start == 10.0
    assert shifted[0].shift == 0.0


def test_s5_5_chapter_touching_a_cut_edge():
    """A chapter exactly at the cut's start is not inside it; one at the end shifts fully."""
    at_start = shift_chapters([Chapter(20.0, "at-start")], [(20.0, 30.0)])
    assert at_start[0].start == 20.0
    at_end = shift_chapters([Chapter(30.0, "at-end")], [(20.0, 30.0)])
    assert at_end[0].start == 20.0


def test_s5_5_chapter_inside_a_cut_is_dropped_or_flagged():
    """A chapter buried in an ad break has nowhere to go; that must be visible."""
    dropped = shift_chapters([Chapter(25.0, "inside")], [(20.0, 30.0)])
    assert dropped == []

    kept = shift_chapters([Chapter(25.0, "inside")], [(20.0, 30.0)], drop_inside_cuts=False)
    assert kept[0].dropped is True
    assert kept[0].start == 20.0


def test_s5_5_chapters_after_a_real_cut_match_the_probed_file(
    marked_episode: BuiltAudio, workdir: Path
):
    """The shifted timeline has to fit inside the audio ffprobe actually measures."""
    plan = plan_cuts([[7.6, 15.1]], marked_episode.path, 1.5)
    out = workdir / "chaptered.wav"
    apply_cuts(marked_episode.path, out, plan.cuts)

    chapters = [Chapter(2.0, "a"), Chapter(18.0, "b"), Chapter(21.5, "c")]
    shifted = shift_chapters(chapters, plan.cuts)
    probed = ffprobe_duration_seconds(out)

    assert shifted[0].start == 2.0
    assert shifted[1].start == pytest.approx(18.0 - plan.total_cut_seconds, abs=1e-9)
    assert all(c.start <= probed + FRAME for c in shifted)


# --------------------------------------------------------------------------------------
# S5.6 -- regression gate against a no-snapping baseline
# --------------------------------------------------------------------------------------


def _jittered(gold: Sequence[Sequence[float]], magnitude: float, rng: random.Random,
              duration: float) -> List[List[float]]:
    """A plausible upstream detector: right ad, edges wrong by up to +-``magnitude``."""
    out: List[List[float]] = []
    for start, end in gold:
        lo = max(0.0, min(duration, start + rng.uniform(-magnitude, magnitude)))
        hi = max(0.0, min(duration, end + rng.uniform(-magnitude, magnitude)))
        if hi > lo:
            out.append([lo, hi])
    return out


def _arm_scores(episodes, magnitude: float, seed: int, tolerance: float = 1.5):
    """Score the no-snapping baseline and the snapping arm over ``episodes``.

    ``episodes`` is a list of ``(path, gold, duration)``.  Both arms see the *same*
    jittered detections, so the only difference between them is the snap-or-refuse step.
    """
    rng = random.Random(seed)
    baseline = {"missed": 0.0, "false_cut": 0.0, "combined": 0.0}
    snapped = {"missed": 0.0, "false_cut": 0.0, "combined": 0.0}
    refusals = 0
    proposals = 0
    for path, gold, duration in episodes:
        raw = _jittered(gold, magnitude, rng, duration)
        proposals += len(raw)
        plan = plan_cuts(raw, path, tolerance)
        refusals += plan.refusal_count
        for bucket, pred in ((baseline, raw), (snapped, plan.cuts)):
            result = score(pred, gold)
            bucket["missed"] += result.missed_ad_seconds
            bucket["false_cut"] += result.false_cut_seconds
            bucket["combined"] += result.combined_score
    return baseline, snapped, refusals, proposals


def _report(title: str, rows: Sequence[Sequence[object]]) -> None:
    print(f"\n--- {title} ---")
    for row in rows:
        print("  " + "  ".join(str(cell) for cell in row))


def test_s5_6_scores_on_the_step1_synthetic_corpus(step1_manifest: dict):
    """S5.6, measured on the shipped step-1 corpus.  Reports both numbers.

    Honest finding, asserted here rather than merely written down: **the step-1 corpus
    contains no silence anywhere** -- it is continuous synthesised tone and noise -- so
    the snapping arm refuses every segment and cuts nothing.  Its asymmetric score is
    therefore exactly the "cut nothing" score, i.e. all gold ad seconds missed.

    Whether that is better or worse than cutting on unsnapped edges depends entirely on
    how wrong the detector is, so the sweep below reports the whole curve instead of
    picking a flattering point.
    """
    episodes = []
    for ep in step1_manifest["episodes"]:
        path = CORPUS_DIR / ep["path"]
        episodes.append((path, gold_intervals(ep), ep["duration_seconds"]))
    assert episodes, "empty corpus"

    # the premise of the finding, checked rather than assumed
    total_regions = sum(len(detect_silences(path)) for path, _, _ in episodes)
    assert total_regions == 0, "step-1 corpus unexpectedly contains silence"

    total_gold = sum(sum(e - s for s, e in gold) for _, gold, _ in episodes)

    rows = [("jitter_s", "baseline_combined", "snapping_combined", "refusals/proposals")]
    results: Dict[float, Tuple[float, float]] = {}
    for magnitude in (0.25, 0.5, 1.0, 2.0, 4.0):
        baseline, snapped, refusals, proposals = _arm_scores(episodes, magnitude, seed=515)
        results[magnitude] = (baseline["combined"], snapped["combined"])
        rows.append(
            (
                f"{magnitude:>8.2f}",
                f"{baseline['combined']:>17.3f}",
                f"{snapped['combined']:>17.3f}",
                f"{refusals}/{proposals}",
            )
        )
        # every segment is refused, so the snapping arm's score is the all-missed score
        assert refusals == proposals
        assert snapped["combined"] == pytest.approx(total_gold, abs=1e-6)
        assert snapped["false_cut"] == 0.0

    _report(
        f"S5.6 step-1 corpus (no silence anywhere); total gold ad seconds = "
        f"{total_gold:.2f}; FALSE_CUT_WEIGHT = {FALSE_CUT_WEIGHT}",
        rows,
    )

    # The crossover is a real, measured property of the asymmetric score: refusing to cut
    # beats cutting badly once the detector is sloppy enough.
    assert results[0.25][0] < results[0.25][1], "expected baseline to win at small jitter"
    assert results[4.0][1] < results[4.0][0], "expected refusal to win at large jitter"


@pytest.fixture(scope="module")
def silence_marked_corpus(workdir: Path):
    """Six episodes whose ad breaks are bounded by pauses, as real speech is.

    Ground truth is exact by construction.  The gold interval for a break is the whole
    break *including the pauses that bracket it* -- that is what a human labeller marks,
    because the pause belongs to the break, not to the sentence before it.  The stricter
    "creative audio only" gold is reported alongside in the test, so the choice is
    visible rather than convenient.
    """
    rng = random.Random(9090)
    episodes = []
    for index in range(6):
        spec: List[Tuple[str, float, str]] = []
        n_ads = rng.choice([1, 2, 2, 3])
        for slot in range(n_ads + 1):
            spec.append(("content", rng.randrange(60, 140) / 10.0, TONE.format(freq=150 + 10 * slot)))
            if slot < n_ads:
                spec.append(("gap", 0.4, ROOM_TONE.format(seed=100 + slot)))
                spec.append(("ad", rng.randrange(50, 90) / 10.0, TONE.format(freq=900 + 50 * slot)))
                spec.append(("gap", 0.4, ROOM_TONE.format(seed=200 + slot)))
        built = _build(spec, workdir / f"marked_{index:02d}.wav")
        # gold including the bracketing pauses
        broad = [
            [ad_start - 0.4, ad_end + 0.4] for ad_start, ad_end in built.ads
        ]
        episodes.append({"built": built, "broad": broad, "strict": [list(a) for a in built.ads]})
    return episodes


def test_s5_6_regression_gate_on_silence_marked_audio(silence_marked_corpus):
    """S5.6 gate: on audio whose breaks have real edges, snapping must not worsen the score.

    Both arms consume the identical jittered detections; the only difference is snapping.
    Both gold definitions are reported.
    """
    broad = [(e["built"].path, e["broad"], e["built"].duration) for e in silence_marked_corpus]
    strict = [(e["built"].path, e["strict"], e["built"].duration) for e in silence_marked_corpus]

    rows = [("gold", "jitter_s", "baseline_combined", "snapping_combined", "refusals/proposals")]
    gate_checked = 0
    for magnitude in (0.25, 0.5, 1.0):
        b_base, b_snap, b_ref, b_prop = _arm_scores(broad, magnitude, seed=77)
        s_base, s_snap, s_ref, s_prop = _arm_scores(strict, magnitude, seed=77)
        rows.append(("break+pauses", f"{magnitude:.2f}",
                     f"{b_base['combined']:.3f}", f"{b_snap['combined']:.3f}",
                     f"{b_ref}/{b_prop}"))
        rows.append(("creative-only", f"{magnitude:.2f}",
                     f"{s_base['combined']:.3f}", f"{s_snap['combined']:.3f}",
                     f"{s_ref}/{s_prop}"))
        assert b_snap["combined"] <= b_base["combined"], (
            f"snapping worsened the score at jitter {magnitude}: "
            f"{b_snap['combined']} > {b_base['combined']}"
        )
        # the expensive error is the one snapping is for
        assert b_snap["false_cut"] <= b_base["false_cut"]
        gate_checked += 1

    _report(
        f"S5.6 silence-marked corpus ({len(broad)} episodes); FALSE_CUT_WEIGHT = "
        f"{FALSE_CUT_WEIGHT}",
        rows,
    )
    assert gate_checked == 3


# --------------------------------------------------------------------------------------
# input validation -- nothing here silently repairs bad input
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boundary,tolerance",
    [(float("nan"), 1.0), (float("inf"), 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -1.0),
     ("10", 1.0), (True, 1.0)],
)
def test_invalid_snap_input_raises(boundary, tolerance):
    with pytest.raises(BoundaryError):
        snap(boundary, [(0.0, 1.0)], tolerance)


def test_probe_missing_file_raises(tmp_path: Path):
    with pytest.raises(BoundaryError, match="no such audio file"):
        probe_audio(tmp_path / "nope.wav")


def test_silence_map_from_intervals_merges_and_sorts():
    smap = SilenceMap.from_intervals([(5.0, 6.0), (0.0, 1.0), (1.0, 2.0)])
    assert [r.as_tuple() for r in smap.regions] == [(0.0, 2.0), (5.0, 6.0)]


def test_snap_guard_keeps_the_boundary_clear_of_the_silence_edges():
    """The guard is why a snapped cut never lands on the exact instant speech resumes."""
    decision = snap(20.0, [(9.0, 11.0)], 15.0, edge_guard_seconds=0.05)
    assert decision.snapped == pytest.approx(11.0 - 0.05)
    # a silence shorter than twice the guard collapses to its midpoint, never past it
    short = snap(20.0, [(9.0, 9.02)], 15.0, edge_guard_seconds=DEFAULT_EDGE_GUARD_SECONDS)
    assert 9.0 <= short.snapped <= 9.02
