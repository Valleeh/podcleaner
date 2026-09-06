"""Offline tests for the screen-then-verify cascade."""

from __future__ import annotations

import json

from podcleaner.detect.cascade import CascadeClassifier, CascadeConfig, candidate_windows
from podcleaner.detect.llm import AdSegment, LLMConfig
from podcleaner.transcripts import Cue, Transcript


def _cues(n, seconds_each=4.0):
    return [Cue(i + 1, i * seconds_each, (i + 1) * seconds_each, f"line {i + 1}") for i in range(n)]


def _seg(a, b, cat="sponsor_read", conf=0.5):
    return AdSegment(int(a // 4) + 1, int(b // 4), a, b, cat, conf, "")


def test_candidate_windows_cluster_nearby_segments():
    segs = [_seg(100, 130), _seg(140, 160), _seg(500, 520)]
    assert candidate_windows(segs, gap=45) == [(100, 160), (500, 520)]
    assert candidate_windows(segs, gap=5) == [(100, 130), (140, 160), (500, 520)]
    assert candidate_windows([], gap=45) == []


def test_cascade_verifies_only_around_candidates_and_uses_the_verifier_answer():
    cues = _cues(600)  # 40 minutes
    screen_prompts, verify_prompts = [], []

    def screen(system, user):
        screen_prompts.append((system, user))
        # over-eager screen: a real ad at cues 101-110 and a false alarm at cues 400-402
        return json.dumps({"segments": [
            {"start_cue": 101, "end_cue": 110, "category": "sponsor_read", "confidence": 0.3, "reason": "maybe"},
            {"start_cue": 400, "end_cue": 402, "category": "cross_promo", "confidence": 0.2, "reason": "maybe"},
        ]}), {"prompt_tokens": 1000, "completion_tokens": 10}

    def verify(system, user):
        verify_prompts.append(user)
        first = int(user.split("\n[", 1)[1].split("]", 1)[0])
        if first < 200:
            # confirms the ad and tightens its end by one cue
            return json.dumps({"segments": [{"start_cue": 101, "end_cue": 109, "category": "sponsor_read",
                                             "confidence": 0.95, "reason": "confirmed"}]}), {"prompt_tokens": 200, "completion_tokens": 5}
        return json.dumps({"segments": []}), {"prompt_tokens": 200, "completion_tokens": 5}

    cfg = CascadeConfig(screen=LLMConfig(api_key="x"), verify=LLMConfig(api_key="x", model="verifier"), context_seconds=60)
    clf = CascadeClassifier(cfg, screen_complete=screen, verify_complete=verify)
    analysis = clf.classify(Transcript(cues))

    assert "screening pass" in screen_prompts[0][0], "the screen is told to over-report"
    assert len(verify_prompts) == 2 and all("Excerpt of a longer episode" in p for p in verify_prompts)
    # the verifier saw only the neighbourhood: cue 101 is at 400 s, so the window starts near 340 s (cue 86)
    first_idx = int(verify_prompts[0].split("\n[", 1)[1].split("]", 1)[0])
    assert 80 <= first_idx <= 90
    assert [(s.start_cue, s.end_cue, s.category) for s in analysis.segments] == [(101, 109, "sponsor_read")]
    assert analysis.cut_intervals("promos") == [(400.0, 436.0)]
    assert clf.calls == 3 and analysis.calls == 3
    assert analysis.prompt_tokens == 1400 and clf.verify_tokens == 400
    assert analysis.degraded is False


def test_cascade_with_no_candidates_makes_no_verify_calls():
    cues = _cues(50)
    verify_calls = []
    cfg = CascadeConfig(screen=LLMConfig(api_key="x"), verify=LLMConfig(api_key="x"))
    clf = CascadeClassifier(cfg, screen_complete=lambda s, u: (json.dumps({"segments": []}), {}),
                            verify_complete=lambda s, u: verify_calls.append(u) or ("{}", {}))
    analysis = clf.classify(Transcript(cues))
    assert analysis.segments == [] and verify_calls == [] and clf.calls == 1


def test_spec_parsing_and_factory(monkeypatch):
    from podcleaner.detect.cascade import classifier_from_spec, parse_spec
    from podcleaner.detect.llm import AdClassifier

    assert parse_spec("qwen/x") == (["qwen/x"], None)
    assert parse_spec("cascade:a>b") == (["a"], "b")
    assert parse_spec("cascade:a+c>b") == (["a", "c"], "b")
    import pytest as _pytest
    with _pytest.raises(ValueError):
        parse_spec("cascade:>b")
    base = LLMConfig(api_key="x", base_url="http://local:1234/v1")
    single = classifier_from_spec("qwen/x", base_config=base)
    assert isinstance(single, AdClassifier) and single.config.model == "qwen/x"
    casc = classifier_from_spec("cascade:a+c>b", base_config=base)
    assert isinstance(casc, CascadeClassifier)
    assert [c.model for c in casc.config.screens] == ["a", "c"] and casc.config.verify.model == "b"
    monkeypatch.setenv("PODCLEANER_LLM_SPEC", "cascade:s>v")
    env_casc = classifier_from_spec(base_config=base)
    assert isinstance(env_casc, CascadeClassifier) and env_casc.config.verify.model == "v"
    monkeypatch.delenv("PODCLEANER_LLM_SPEC")
    from podcleaner.detect.cascade import DEFAULT_SPEC
    default = classifier_from_spec(base_config=base)
    assert isinstance(default, CascadeClassifier) and DEFAULT_SPEC.startswith("cascade:")
