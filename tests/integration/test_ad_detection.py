"""Integration tests for the LLM ad classifier on real episodes.

Separated from transcription on purpose: each case feeds the classifier a *stored*
transcript, so a whisper regression cannot masquerade as a classification regression and
the LLM tests do not need hours of CPU.  Three transcript sources are used:

``hybrid``
    Publisher transcript of the clean master, mapped into the listener file's timeline,
    plus whisper transcripts of exactly the server-inserted regions.  Editorial text is
    publisher quality; the ad text is what whisper heard.  Ground truth for the inserted
    regions is exact (constructed from the two files); host reads are text-derived labels.
``whisper``
    A whole-episode whisper transcript of the file the labels describe (Hacks on Tap;
    others once ``--full`` has produced them).
``whisper-clean``
    Whisper transcript of a clean master that contains **no advertising at all**: the
    negative control.  Any cut here is content destroyed.

Gates follow quality goal 1: cutting editorial more than a few seconds from any ad edge
fails outright; missing ads is measured and gated on a recall floor.
"""

from __future__ import annotations

import json

import pytest

from podcleaner.detect.llm import POLICIES
from podcleaner.eval.adscore import evaluate
from podcleaner.eval.fixtures import load_manifest
from podcleaner.eval.labels import LabelError, gold_ads, load_label
from podcleaner.transcripts import Transcript, load_transcript

from .support import INTEGRATION_DIR, REPORTS_DIR, dont_care_from_transcript, gate, load_dai, require, stitched_transcript

pytestmark = [pytest.mark.integration, pytest.mark.llm]

_MANIFEST = load_manifest()

POLICY = "promos"
MIN_CONFIDENCE = 0.5
EDGE_TOLERANCE = 3.0      # seconds of slop allowed around a labelled edge before it counts as content destroyed
RECALL_FLOOR = 0.90       # of in-policy ad seconds that lie under a transcript cue (a cue-aligned
                          # classifier cannot cut the jingle after an ad's last word)
MAX_FALSE_CUT_PER_SEGMENT = 2 * EDGE_TOLERANCE

CASES = [
    pytest.param("ldn491", "hybrid", id="ldn491-hybrid"),
    pytest.param("ldn490", "hybrid", id="ldn490-hybrid"),
    pytest.param("solved-life-path", "hybrid", id="solved-hybrid"),
    pytest.param("hot-oh-canada", "whisper", id="hot-whisper"),
    pytest.param("ldn491", "whisper", id="ldn491-whisper", marks=pytest.mark.full),
    pytest.param("solved-life-path", "whisper", id="solved-whisper", marks=pytest.mark.full),
]


def _label_for(ep, store, provisional_ok):
    label = load_label(INTEGRATION_DIR / ep.label)
    audio = require(store, ep.audio[label["episode"]["variant"]])
    try:
        gold = gold_ads(label, allow_provisional=provisional_ok, audio_sha256=ep.audio[label["episode"]["variant"]].sha256)
    except LabelError as exc:
        pytest.skip(f"labels not usable as gold: {exc} (pass --provisional-labels to measure anyway)")
    return label, gold, audio


def _transcript_for(ep, source, store, transcriber_factory):
    if source == "hybrid":
        official = load_transcript(require(store, ep.transcripts["official"]))
        dai = load_dai(INTEGRATION_DIR / ep.dai["file"])
        audio = require(store, ep.audio[ep.dai["stitched"]])
        transcriber = transcriber_factory()
        inserts = [transcriber.transcribe(audio, start=r.start, duration=r.duration, language=ep.language)
                   for r in dai.regions]
        return stitched_transcript(official, dai, inserts)
    if source == "whisper":
        if "whisper-small" in ep.transcripts and ep.transcripts["whisper-small"].meta.get("aligned_to") == "podcatcher":
            return load_transcript(require(store, ep.transcripts["whisper-small"]))
        cached = store.root / "transcripts" / f"{ep.id}.podcatcher.whisper-small.json"
        if not cached.exists():
            pytest.skip(f"no whole-episode whisper transcript of the listener file yet; run --full transcription first ({cached})")
        return Transcript.load(cached)
    raise ValueError(source)


def _write_artifacts(report_dir, eid, source, analysis, evaluation):
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"ads-{eid}-{source}.json").write_text(json.dumps({
        "analysis": analysis.to_dict(policy=POLICY, min_confidence=MIN_CONFIDENCE),
        "evaluation": evaluation.to_dict(),
    }, indent=1, ensure_ascii=False))


@pytest.mark.parametrize("eid,source", CASES)
def test_ads_found_and_no_content_destroyed(eid, source, store, classifier, report, baselines, provisional_ok, request, catalogue, llm_config):
    ep = _MANIFEST[eid]
    label, gold, _audio = _label_for(ep, store, provisional_ok)

    def transcriber_factory():
        return request.getfixturevalue("transcriber")

    transcript = _transcript_for(ep, source, store, transcriber_factory)
    analysis = classifier.classify(transcript)
    cuts = analysis.cut_intervals(POLICY, min_confidence=MIN_CONFIDENCE)
    ev = evaluate(cuts, gold + dont_care_from_transcript(transcript), duration=label["episode"]["duration_seconds"],
                  policy_categories=set(POLICIES[POLICY]), edge_tolerance=EDGE_TOLERANCE,
                  coverable=[(c.start, c.end) for c in transcript.cues])
    key = f"ads:{eid}:{source}"
    cost = classifier.cost_fn(analysis, classifier)
    report.add("ads", key, model=classifier.spec, cost_usd=cost, recall=ev.recall, coverable_recall=ev.coverable_recall,
               precision=ev.precision, false_cut=ev.false_cut_seconds,
               false_cut_outside=ev.false_cut_outside_tolerance_seconds, missed=ev.missed_seconds,
               segments=f"{ev.segments_found}/{len(ev.segments)}", combined=ev.combined_score,
               calls=analysis.calls, chunks=analysis.chunks, prompt_tokens=analysis.prompt_tokens,
               reasoning_tokens=analysis.reasoning_tokens, warnings=len(analysis.warnings), degraded=analysis.degraded,
               label_status=label["status"])
    baselines.observe(key, {"model": classifier.spec, "cost_usd": cost, "combined_score": ev.combined_score,
                            "recall": ev.recall, "coverable_recall": ev.coverable_recall,
                            "false_cut_seconds": ev.false_cut_seconds})
    _write_artifacts(REPORTS_DIR / report.started, eid, source, analysis, ev)
    print(f"\n[{key}] model={analysis.model} calls={analysis.calls}\n{ev.summary()}")
    for w in analysis.warnings:
        print(f"  warning: {w}")

    assert not analysis.degraded, "the model's reply stayed corrupted after a retry:\n" + "\n".join(analysis.warnings)
    # quality goal 1: never remove real content
    assert ev.false_cut_outside_tolerance_seconds == 0, (
        f"{ev.false_cut_outside_tolerance_seconds:.1f}s of editorial cut more than {EDGE_TOLERANCE:.0f}s from any "
        f"labelled ad edge:\n{ev.summary()}")
    assert ev.false_cut_seconds <= MAX_FALSE_CUT_PER_SEGMENT * max(1, len(ev.segments)), (
        f"{ev.false_cut_seconds:.1f}s of boundary slop across {len(ev.segments)} segments")
    # the ads themselves
    assert ev.segments_found == len(ev.segments), "not every labelled ad was found:\n" + ev.summary()
    assert ev.coverable_recall >= RECALL_FLOOR, (
        f"recall {ev.coverable_recall:.1%} of cue-covered ad seconds below {RECALL_FLOOR:.0%}:\n{ev.summary()}")
    problem = gate(baselines.get(key), "combined_score", ev.combined_score, margin=5.0)
    assert problem is None, problem


def test_clean_master_with_no_ads_gets_no_cuts(store, classifier, report, baselines):
    """Negative control.  LdN490's clean master has no advertising (publisher transcript
    and file duration agree, and the whisper transcript contains none).  Every cut the
    classifier proposes here is content destroyed."""
    ep = _MANIFEST["ldn490"]
    transcript = load_transcript(require(store, ep.transcripts["whisper-small"]))
    analysis = classifier.classify(transcript)
    cuts = analysis.cut_intervals(POLICY, min_confidence=MIN_CONFIDENCE)
    cut_seconds = sum(b - a for a, b in cuts)
    key = "ads:ldn490:whisper-clean-negative"
    report.add("ads", key, cut_seconds=cut_seconds, segments_reported=len(analysis.segments),
               calls=analysis.calls, warnings=len(analysis.warnings))
    baselines.observe(key, {"cut_seconds": cut_seconds})
    _write_artifacts(REPORTS_DIR / report.started, "ldn490", "whisper-clean-negative", analysis,
                     evaluate(cuts, [], duration=transcript.end))
    for s in analysis.segments:
        print(f"  reported: {s.start:.1f}-{s.end:.1f} {s.category} conf={s.confidence} {s.reason[:80]}")
    assert cuts == [], f"{cut_seconds:.1f}s would be cut from an episode with no ads: {cuts}"


def test_inserted_regions_reconcile_with_file_durations():
    """The constructed ground truth checks itself: inserted seconds must equal the
    difference between the two files' durations (both from independent frame walks),
    and every labelled constructed segment must be one of those regions."""
    for eid, ep in _MANIFEST.items():
        if not ep.dai:
            continue
        dai = load_dai(INTEGRATION_DIR / ep.dai["file"])
        assert abs(dai.reconciled_inserted - dai.duration_delta) < 0.05, eid
        assert abs(dai.stitched_duration - ep.audio[ep.dai["stitched"]].duration) < 0.5, eid
        assert abs(dai.clean_duration - ep.audio[ep.dai["clean"]].duration) < 0.5, eid
        label = load_label(INTEGRATION_DIR / ep.label)
        constructed = {(a["start"], a["end"]) for a in label["ads"] if a["source"] == "construction"}
        assert constructed == {(round(r.start, 3), round(r.end, 3)) for r in dai.regions}, eid
