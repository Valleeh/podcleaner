"""Offline tests for podcleaner.detect.llm: chunking, resolution, merging, policy.

Every test here runs with a canned or counting completion function.  The negative
controls matter most: garbage from the model must never become a cut.
"""

from __future__ import annotations

import json

import pytest

from podcleaner.detect.llm import (
    DEFAULT_MAX_CUT_DURATION,
    reply_looks_corrupted,
    AdClassifier,
    AdSegment,
    LLMConfig,
    LLMError,
    chunk_cues,
    estimate_tokens,
    merge_segments,
    parse_model_json,
    render_transcript,
    resolve_segments,
)
from podcleaner.transcripts import Cue, Transcript


def _cues(n: int, seconds_each: float = 4.0, text: str = "some spoken words here") -> list:
    return [Cue(i + 1, i * seconds_each, (i + 1) * seconds_each, f"{text} {i + 1}") for i in range(n)]


def _reply(segments: list) -> str:
    return json.dumps({"segments": segments})


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "raw",
    [
        '{"segments": []}',
        '```json\n{"segments": []}\n```',
        'Sure! Here you go:\n{"segments": []}\nHope that helps.',
    ],
)
def test_parse_model_json_tolerates_fences_and_prose(raw):
    assert parse_model_json(raw) == {"segments": []}


@pytest.mark.parametrize("raw", ["", "no json here", "[1, 2]", "{broken"])
def test_parse_model_json_rejects_garbage(raw):
    with pytest.raises(LLMError):
        parse_model_json(raw)


# ---------------------------------------------------------------- resolution


def test_resolve_uses_our_timestamps_not_the_models():
    cues = _cues(10)
    segs, warnings = resolve_segments(
        {"segments": [{"start_cue": 3, "end_cue": 5, "category": "sponsor_read", "confidence": 0.9, "reason": "x"}]},
        cues,
    )
    assert warnings == []
    assert len(segs) == 1
    assert (segs[0].start, segs[0].end) == (cues[2].start, cues[4].end) == (8.0, 20.0)


def test_resolve_drops_out_of_range_instead_of_clamping():
    """Negative control.  v1 clamped 99999 to the last cue, turning one bad number into a
    cut spanning the episode.  That must not happen."""
    cues = _cues(10)
    segs, warnings = resolve_segments(
        {"segments": [{"start_cue": 99999, "end_cue": 5, "category": "sponsor_read", "confidence": 1.0, "reason": ""}]},
        cues,
    )
    assert segs == []
    assert any("dropped" in w for w in warnings)


def test_resolve_repairs_swapped_bad_category_and_confidence():
    cues = _cues(10)
    segs, warnings = resolve_segments(
        {"segments": [{"start_cue": 6, "end_cue": 4, "category": "nonsense", "confidence": 7, "reason": ""}]},
        cues,
    )
    assert len(segs) == 1
    assert (segs[0].start_cue, segs[0].end_cue) == (4, 6)
    assert segs[0].category == "sponsor_read"
    assert segs[0].confidence == 1.0
    assert len(warnings) == 3


def test_resolve_requires_segments_list():
    with pytest.raises(LLMError):
        resolve_segments({"foo": 1}, _cues(3))
    with pytest.raises(LLMError):
        resolve_segments({"segments": []}, [])


# ---------------------------------------------------------------- merging


def _seg(start, end, cat="sponsor_read", conf=0.9, chunk=0):
    return AdSegment(int(start // 4) + 1, int(end // 4), start, end, cat, conf, "", chunk)


def test_merge_keeps_adjacent_distinct_ads_apart():
    merged, warnings = merge_segments([_seg(0, 30), _seg(30, 60, "cross_promo")])
    assert [(s.start, s.end) for s in merged] == [(0, 30), (30, 60)]
    assert warnings == []


def test_merge_overlapping_segments_with_warning():
    merged, warnings = merge_segments([_seg(0, 30), _seg(20, 50)])
    assert [(s.start, s.end) for s in merged] == [(0, 50)]
    assert len(warnings) == 1


def test_merge_duplicate_from_overlapping_chunk_is_silent():
    merged, warnings = merge_segments([_seg(100, 130, chunk=0), _seg(100, 130, chunk=1)])
    assert len(merged) == 1 and warnings == []


def test_implausibly_long_segment_never_swallows_a_neighbour():
    """Negative control: a whole-episode 'ad' must not merge with and hide a real one."""
    merged, warnings = merge_segments([_seg(0, 4000, "cross_promo"), _seg(900, 990)])
    assert any(s.duration == 90 for s in merged), "the real ad survived"
    assert any("never cut" in w for w in warnings)


# ---------------------------------------------------------------- chunking


def test_chunking_covers_everything_with_overlap_and_global_indices():
    cues = _cues(500, text="a fairly long line of transcript text to make the estimate realistic")
    chunks = chunk_cues(cues, max_tokens=3000, overlap_cues=10)
    assert len(chunks) > 1
    assert chunks[0][0].index == 1 and chunks[-1][-1].index == 500
    for a, b in zip(chunks, chunks[1:]):
        assert b[0].index == a[-1].index - 10 + 1, "each chunk restarts overlap_cues before the previous end"
        assert estimate_tokens(render_transcript(a)) <= 3000 + 100
    assert chunk_cues(cues, max_tokens=10**9, overlap_cues=10) == [cues]
    assert chunk_cues([], max_tokens=100, overlap_cues=0) == []


# ---------------------------------------------------------------- classifier


def test_classifier_counts_calls_and_resolves_against_each_chunk():
    cues = _cues(300, text="quite a long line so that chunking kicks in at a low token budget")
    seen = []

    def fake(system, user):
        seen.append(user)
        first = int(user.split("[", 1)[1].split("]", 1)[0]) if "[" in user else 1
        return _reply([{"start_cue": first, "end_cue": first + 1, "category": "cross_promo", "confidence": 0.8, "reason": ""}]), {}

    clf = AdClassifier(LLMConfig(api_key="x", chunk_tokens=3000, overlap_cues=5), complete=fake)
    analysis = clf.classify(Transcript(cues))
    assert clf.calls == analysis.calls == analysis.chunks == len(seen) > 1
    assert all(s.category == "cross_promo" for s in analysis.segments)
    assert "Excerpt 1 of" in seen[0]


def test_classifier_zero_calls_on_empty_transcript():
    calls = []
    clf = AdClassifier(LLMConfig(api_key="x"), complete=lambda s, u: (calls.append(u) or _reply([]), {}))
    analysis = clf.classify(Transcript([]))
    assert analysis.segments == [] and clf.calls == 0 and calls == []


def test_cut_policy_and_max_duration():
    analysis_segments = [
        _seg(0, 30, "sponsor_read", 0.9),
        _seg(40, 50, "self_promo", 0.9),
        _seg(60, 70, "sponsor_read", 0.2),           # hesitant: reported, not cut
        _seg(100, 100 + DEFAULT_MAX_CUT_DURATION + 1, "cross_promo", 0.99),  # implausible
    ]
    from podcleaner.detect.llm import AdAnalysis

    a = AdAnalysis(segments=analysis_segments)
    assert a.cut_intervals("promos") == [(0, 30)]
    assert a.cut_intervals("all") == [(0, 30), (40, 50)]
    assert a.cut_intervals("promos", min_confidence=0.1) == [(0, 30), (60, 70)]
    d = a.to_dict()
    assert [s["cut"] for s in d["segments"]] == [True, False, False, False]


def test_env_config_resolution(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PODCLEANER_LLM_API_KEY", raising=False)
    cfg = LLMConfig.from_env(secrets_path="")  # no secrets file lookup
    with pytest.raises(LLMError) as exc:
        cfg.resolved_api_key()
    assert exc.value.kind == "auth"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert LLMConfig.from_env(secrets_path="").resolved_api_key() == "sk-test"
    assert LLMConfig(base_url="http://desktop:1234/v1").resolved_api_key() == "not-needed"


def test_secrets_file_fallback(monkeypatch, tmp_path):
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "PODCLEANER_LLM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    secret = tmp_path / ".secret.json"
    secret.write_text('{"openrouter-token": "sk-or-from-file"}')
    assert LLMConfig.from_env(secrets_path=str(secret)).resolved_api_key() == "sk-or-from-file"
    assert LLMConfig.from_env(secrets_path=str(tmp_path / "missing.json")).api_key is None
    secret.write_text("not json")
    assert LLMConfig.from_env(secrets_path=str(secret)).api_key is None


# ------------------------------------------------------------- corrupted replies

CORRUPTED_REPLY = (
    '{"segments": [{"start_cue": 35, "end_cue": 42, "category": "sponsor_read", "confidence": 0.98, '
    '"reason": "Explicit marker and URL.\'} ,{\\"start_cue\\": 555, \\"end_cue\\": 560, \\"category\\": '
    '\\"sponsor_read\\", \\"confidence\\": 0.97, \\"reason\\": \\"inserted ad\\"}]}      ,{"}, '
    '{"start_cue":0,"end_cue":0,"category":"credits","confidence":0,"reason":"placeholder"}]}'
)


def test_reply_looks_corrupted_detects_embedded_json_and_placeholders():
    raw = parse_model_json(CORRUPTED_REPLY)
    assert len(raw["segments"]) == 2, "json.loads accepts it, which is the whole problem"
    assert reply_looks_corrupted(raw) is not None
    assert reply_looks_corrupted({"segments": [{"start_cue": 0, "end_cue": 0, "category": "credits",
                                                "confidence": 0, "reason": "x"}]}) is not None
    assert reply_looks_corrupted({"segments": [{"start_cue": 3, "end_cue": 5, "category": "sponsor_read",
                                                "confidence": 0.9, "reason": "mentions segments of society"}]}) is None
    assert reply_looks_corrupted({"segments": []}) is None


def test_classifier_retries_a_corrupted_reply_with_a_distinct_prompt():
    """This is the exact failure seen live: two ads lost inside a 'reason' string.  The
    retry must use a different prompt (so a reply cache cannot hand back the same
    corrupted text) and the good reply wins."""
    cues = _cues(600)
    good = _reply([
        {"start_cue": 35, "end_cue": 42, "category": "sponsor_read", "confidence": 0.98, "reason": "a"},
        {"start_cue": 555, "end_cue": 560, "category": "sponsor_read", "confidence": 0.97, "reason": "b"},
    ])
    prompts = []

    def flaky(system, user):
        prompts.append(user)
        return (CORRUPTED_REPLY if len(prompts) == 1 else good), {}

    clf = AdClassifier(LLMConfig(api_key="x"), complete=flaky)
    analysis = clf.classify(Transcript(cues))
    assert clf.calls == 2 and prompts[0] != prompts[1] and "Retry" in prompts[1]
    assert [(s.start_cue, s.end_cue) for s in analysis.segments] == [(35, 42), (555, 560)]
    assert analysis.degraded is False
    assert any("corrupted" in w and "retried" in w for w in analysis.warnings)


def test_classifier_gives_up_after_retry_and_marks_result_degraded():
    cues = _cues(600)
    clf = AdClassifier(LLMConfig(api_key="x"), complete=lambda s, u: (CORRUPTED_REPLY, {}))
    analysis = clf.classify(Transcript(cues))
    assert clf.calls == 2 and analysis.degraded is True
    assert [(s.start_cue, s.end_cue) for s in analysis.segments] == [(35, 42)], "salvageable part kept"
    assert any("dropped" in w for w in analysis.warnings)  # the 0-0 placeholder
