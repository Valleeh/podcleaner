"""Integration tests for the transcriber on real episodes.

What is measured, and against what:

* **WER** of whisper.cpp on 120-second windows of the clean master against the publisher's
  own transcript (Lage der Nation, Solved).  Those references are machine transcripts of
  clean studio tracks -- silver, not gold -- so the number mixes our errors with theirs;
  it is nevertheless stable and repeatable, which is what a regression gate needs.
* **Timing**: where whisper puts the first words of each reference cue, relative to the
  reference.  Cuts are made on timestamps, so this matters more than spelling.
* **Ad anchors**: the first and last line of every labelled ad inside a window must be
  recognisable in whisper's output at the right time, because the LLM tier can only
  find what the transcript contains.
* **Coverage / hallucination**: speech time is not dropped, and nothing is invented
  on silence or noise (negative controls).

Whole-episode work is under ``--full``.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from podcleaner.eval.fixtures import load_manifest
from podcleaner.eval.labels import gold_ads, load_label
from podcleaner.eval.wer import anchor_offsets, find_phrase, inner_wer, normalize_words, window_wer, word_error_rate
from podcleaner.transcripts import Transcript, is_non_speech, load_transcript

from .support import INTEGRATION_DIR, gate, load_dai, require, synth_wav

pytestmark = [pytest.mark.integration, pytest.mark.whisper]

_MANIFEST = load_manifest()

#: Absolute ceilings a *working* transcriber must stay under regardless of baselines.
#: They are deliberately loose; the tight gates are the recorded baselines.
WER_CEILING = {"de": 0.30, "en": 0.25}
ANCHOR_P90_CEILING = 1.5   # seconds
ANCHOR_FOUND_FLOOR = 0.5   # fraction of reference cues whose first 4 words are found
COVERAGE_SLACK = 0.20      # hyp speech coverage may be this much below the reference's
MAX_CUE_SECONDS = 30.0
MAX_REPEATS = 3            # identical consecutive cues before we call it a loop


def _windows(purpose):
    out = []
    for eid, ep in _MANIFEST.items():
        for w in ep.windows:
            if w["purpose"] == purpose:
                out.append(pytest.param(eid, w, id=f"{eid}-{w['id']}"))
    return out


def _transcribe_window(transcriber, store, ep, w):
    audio = require(store, ep.audio[w["variant"]])
    return transcriber.transcribe(audio, start=w["start"], duration=w["duration"], language=ep.language)


def _official(store, ep):
    if "official" not in ep.transcripts:
        pytest.skip(f"{ep.id} has no publisher transcript")
    return load_transcript(require(store, ep.transcripts["official"]))


# ---------------------------------------------------------------------------- WER


@pytest.mark.parametrize("eid,w", _windows("wer"))
def test_window_wer_against_publisher_transcript(eid, w, store, transcriber, report, baselines):
    ep = _MANIFEST[eid]
    ref = _official(store, ep)
    hyp = _transcribe_window(transcriber, store, ep, w)
    start, end = w["start"], w["start"] + w["duration"]
    res = window_wer(ref, hyp, start, end)
    key = f"wer:{eid}:{w['id']}"
    metrics = {"wer": res.wer, "ref_words": res.reference_words, "hyp_words": res.hypothesis_words,
               "sub": res.substitutions, "del": res.deletions, "ins": res.insertions,
               "realtime_factor": hyp.meta.get("realtime_factor"), "language": hyp.language}
    report.add("wer", key, **metrics)
    baselines.observe(key, {"wer": res.wer})

    assert res.reference_words >= 150, "window too short to measure a rate"
    assert hyp.language == ep.language, f"language detected as {hyp.language!r}, expected {ep.language!r}"
    assert res.wer <= WER_CEILING[ep.language], f"WER {res.wer:.3f} above the absolute ceiling"
    problem = gate(baselines.get(key), "wer", res.wer, margin=0.03)
    assert problem is None, problem


# ------------------------------------------------------------------------- timing


@pytest.mark.parametrize("eid,w", _windows("wer"))
def test_window_timing_against_publisher_transcript(eid, w, store, transcriber, report, baselines):
    ep = _MANIFEST[eid]
    ref = _official(store, ep)
    hyp = _transcribe_window(transcriber, store, ep, w)
    start, end = w["start"], w["start"] + w["duration"]
    anchors = anchor_offsets(ref.window(start, end), hyp)
    found = [a for a in anchors if a.hyp_time is not None]
    assert anchors, "reference window has no cue long enough to anchor on"
    deltas = sorted(abs(a.delta) for a in found)
    p50 = statistics.median(deltas) if deltas else float("inf")
    p90 = deltas[int(0.9 * (len(deltas) - 1))] if deltas else float("inf")
    key = f"timing:{eid}:{w['id']}"
    report.add("timing", key, anchors=len(anchors), found=len(found), p50=p50, p90=p90,
               worst=max(deltas) if deltas else None)
    baselines.observe(key, {"p90": p90, "found_fraction": len(found) / len(anchors)})

    assert len(found) / len(anchors) >= ANCHOR_FOUND_FLOOR, (
        f"only {len(found)}/{len(anchors)} reference cue openings were recognised")
    assert p90 <= ANCHOR_P90_CEILING, f"p90 timing error {p90:.2f}s"
    problem = gate(baselines.get(key), "p90", p90, margin=0.3)
    assert problem is None, problem


# ----------------------------------------------------------------- coverage/loops


@pytest.mark.parametrize("eid,w", _windows("wer") + _windows("anchor") + _windows("drift"))
def test_window_coverage_and_no_hallucination_loops(eid, w, store, transcriber, report):
    ep = _MANIFEST[eid]
    hyp = _transcribe_window(transcriber, store, ep, w)
    start, end = w["start"], w["start"] + w["duration"]
    speech = [c for c in hyp.cues if not is_non_speech(c.text)]
    coverage = sum(min(c.end, end) - max(c.start, start) for c in speech) / w["duration"]
    longest = max((c.duration for c in hyp.cues), default=0.0)
    repeats, run = 1, 1
    for a, b in zip(hyp.cues, hyp.cues[1:]):
        run = run + 1 if normalize_words(a.text) == normalize_words(b.text) and normalize_words(a.text) else 1
        repeats = max(repeats, run)
    report.add("coverage", f"coverage:{eid}:{w['id']}", coverage=coverage, cues=len(hyp.cues),
               longest_cue=longest, max_repeats=repeats)

    assert hyp.cues, "no output at all for a window of speech"
    assert longest <= MAX_CUE_SECONDS, f"a {longest:.0f}s cue means whisper lost its timestamps"
    assert repeats < MAX_REPEATS, f"the same line {repeats} times in a row: a hallucination loop"
    if "official" in ep.transcripts and w["variant"] == ep.transcripts["official"].meta.get("aligned_to"):
        ref = _official(store, ep).window(start, end, mode="overlap")
        ref_cov = sum(min(c.end, end) - max(c.start, start) for c in ref.cues) / w["duration"]
        assert coverage >= ref_cov - COVERAGE_SLACK, (
            f"speech coverage {coverage:.2f} vs reference {ref_cov:.2f}: whisper dropped speech")
    else:
        assert coverage >= 0.5, f"speech coverage {coverage:.2f} in a window that should be mostly talk"


# ------------------------------------------------------------------- ad anchors


def _label_transcript_cues(ep, label, store):
    """``{cue_index: start_in_label_timeline}`` for the transcript a label was made on."""
    info = label.get("transcript") or {}
    name = info.get("name")
    if not name or name not in ep.transcripts:
        return {}
    t = load_transcript(require(store, ep.transcripts[name]))
    if info.get("aligned_to") == "clean" and ep.dai:
        dai = load_dai(INTEGRATION_DIR / ep.dai["file"])
        return {c.index: dai.to_stitched(c.start) for c in t.cues}
    return {c.index: c.start for c in t.cues}



@pytest.mark.parametrize("eid,w", _windows("anchor"))
def test_labelled_ad_lines_are_recognisable_at_the_right_time(eid, w, store, transcriber, report, baselines, provisional_ok):
    ep = _MANIFEST[eid]
    label = load_label(INTEGRATION_DIR / ep.label)
    if label["status"] != "complete" and not provisional_ok:
        pytest.skip("labels not yet verified by a human; pass --provisional-labels to measure anyway")
    start, end = w["start"], w["start"] + w["duration"]
    ads = [a for a in label["ads"] if start <= a["start"] < end and a.get("first_line") and not a.get("ambiguous")]
    if not ads:
        pytest.skip("no labelled ad starts inside this window")
    hyp = _transcribe_window(transcriber, store, ep, w)
    label_cues = _label_transcript_cues(ep, label, store)
    misses, deltas = [], []
    for a in ads:
        # Both lines are compared by the START of the cue that carries them: the label's
        # first cue starts at a["start"]; its last cue's start comes from the transcript the
        # label was made on (cue ends differ between a window run and a whole-episode run).
        last_start = label_cues.get(a.get("end_cue"))
        checks = [("first", a["first_line"], a["start"])]
        if last_start is not None and last_start < end:
            checks.append(("last", a["last_line"], last_start))
        for which, line, t in checks:
            if not line or is_non_speech(line):
                continue
            m = find_phrase(hyp, line, near=t, radius=15.0, min_ratio=0.6)
            if m is None:
                misses.append(f"{which} line of {a.get('note') or a['category']} not found: {line!r}")
                continue
            delta = m.start - t
            deltas.append(abs(delta))
            report.add("anchor", f"anchor:{eid}:{w['id']}:{a.get('note', '')[:20]}:{which}",
                       ratio=m.ratio, delta=delta)
    key = f"anchor:{eid}:{w['id']}"
    baselines.observe(key, {"worst_delta": max(deltas) if deltas else None, "misses": len(misses)})
    assert not misses, "\n".join(misses)
    assert max(deltas) <= 3.0, f"an ad boundary line was recognised {max(deltas):.1f}s from where the label puts it"


# ------------------------------------------------------------------------ drift


@pytest.mark.parametrize("eid,w", _windows("drift"))
def test_window_agrees_with_cached_whole_episode_transcript(eid, w, store, transcriber, report, baselines):
    """Same engine, same audio: a window transcript must reproduce the cached whole-episode
    transcript, up to the context whisper had at the window edges."""
    ep = _MANIFEST[eid]
    cached = load_transcript(require(store, ep.transcripts["whisper-small"]))
    hyp = _transcribe_window(transcriber, store, ep, w)
    start, end = w["start"], w["start"] + w["duration"]
    raw = window_wer(cached, hyp, start, end)
    # the cached run segmented the window edges differently; compare the inner words
    inner = inner_wer(cached.window(start - 30, end + 30, mode="overlap").text(), hyp.text())
    key = f"drift:{eid}:{w['id']}"
    report.add("drift", key, inner_wer=inner.wer, raw_wer=raw.wer, ref_words=inner.reference_words)
    baselines.observe(key, {"inner_wer": inner.wer, "raw_wer": raw.wer})
    assert inner.wer <= 0.10, f"window disagrees with the cached transcript by {inner.wer:.1%} away from the edges"
    problem = gate(baselines.get(key), "inner_wer", inner.wer, margin=0.03)
    assert problem is None, problem


# ---------------------------------------------------------------- negative controls


@pytest.mark.parametrize("kind,amplitude", [("silence", 0), ("noise", 300)])
def test_nothing_is_transcribed_from_non_speech(kind, amplitude, transcriber, report, tmp_path):
    """Whisper is known to invent text on silence and noise.  Whatever it invents here
    would be classified and could become a cut, so it must stay negligible."""
    wav = synth_wav(tmp_path / f"{kind}.wav", 30.0, noise_amplitude=amplitude)
    hyp = transcriber.transcribe(wav, language="en", use_cache=False)
    words = [w for c in hyp.cues if not is_non_speech(c.text) for w in normalize_words(c.text)]
    report.add("negative", f"negative:{kind}", words=len(words), text=" ".join(words)[:80])
    assert len(words) <= 2, f"whisper invented {len(words)} words on {kind}: {' '.join(words)!r}"


# ------------------------------------------------------------------------- full


@pytest.mark.full
@pytest.mark.parametrize("eid", ["ldn491", "solved-life-path"])
def test_full_episode_transcript_of_the_listener_file(eid, store, transcriber, report, baselines):
    """Transcribe the whole stitched file (hours) and cache it for the whisper-based ad
    detection case.  WER is computed on editorial cues only, mapped back to the clean
    master's timeline through the DAI offset map."""
    ep = _MANIFEST[eid]
    audio = require(store, ep.audio["podcatcher"])
    out = store.root / "transcripts" / f"{eid}.podcatcher.whisper-small.json"
    if out.exists():
        hyp = Transcript.load(out)
    else:
        hyp = transcriber.transcribe(audio, language=ep.language)
        hyp.save(out)
    ref = _official(store, ep)
    dai = load_dai(INTEGRATION_DIR / ep.dai["file"])
    editorial = [c for c in hyp.cues if dai.to_clean(c.start) is not None]
    hyp_text = " ".join(c.text for c in editorial)
    res = word_error_rate(ref.text(), hyp_text)
    duration = ep.audio["podcatcher"].duration  # decoded length from the manifest; ffprobe's header can lie
    report.add("full", f"full:{eid}", wer=res.wer, cues=len(hyp.cues), last_cue_end=hyp.end, duration=duration,
               realtime_factor=hyp.meta.get("realtime_factor"))
    baselines.observe(f"full:{eid}", {"wer": res.wer})
    assert hyp.end >= duration - 10.0, "transcript ends long before the audio does"
    assert res.wer <= WER_CEILING[ep.language]
