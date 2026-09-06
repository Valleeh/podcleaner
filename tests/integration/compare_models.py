"""Run the ad-detection cases against several models and tabulate cost against quality.

    .venv/bin/python tests/integration/compare_models.py                 # default candidate list
    .venv/bin/python tests/integration/compare_models.py -m a/b -m c/d   # specific models

Uses the same transcripts, labels, gates and reply cache as ``test_ad_detection.py``, so
a model that passes here passes the suite with ``--llm-model``.  Cached replies make a
re-run free; Sonnet's replies from the recorded baseline run are reused the same way.
Writes ``var/reports/model-comparison-<ts>.{json,md}``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from podcleaner.detect.cascade import CascadeClassifier, CascadeConfig  # noqa: E402
from podcleaner.detect.llm import POLICIES, AdClassifier, LLMError, OpenAICompatibleClient  # noqa: E402
from podcleaner.detect.transcribe import TranscriptionError, WhisperCppTranscriber  # noqa: E402
from podcleaner.eval.adscore import evaluate  # noqa: E402
from podcleaner.eval.fixtures import FixtureStore, load_manifest  # noqa: E402
from podcleaner.eval.labels import gold_ads, load_label  # noqa: E402
from podcleaner.transcripts import load_transcript  # noqa: E402
from tests.integration.support import (  # noqa: E402
    CACHE_DIR, INTEGRATION_DIR, REPORTS_DIR, CachedCompletion, config_for_model, dont_care_from_transcript,
    load_catalogue, load_dai, stitched_transcript,
)
from tests.integration.test_ad_detection import EDGE_TOLERANCE, MAX_FALSE_CUT_PER_SEGMENT, MIN_CONFIDENCE, POLICY, RECALL_FLOOR  # noqa: E402

DEFAULT_MODELS = [
    "anthropic/claude-sonnet-5",            # reference (cached replies)
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "openai/gpt-5-nano",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash",
    "deepseek/deepseek-v4-flash",
    "qwen/qwen3.7-flash",
    "qwen/qwen3-30b-a3b-instruct-2507",
    "mistralai/mistral-small-3.2-24b-instruct",
    "z-ai/glm-4.7-flash",
    "nvidia/nemotron-3.5-lightning",
    "bytedance-seed/seed-1.6-flash",
    "google/gemma-4-26b-a4b-it",
]

CASES = [("ldn491", "hybrid"), ("ldn490", "hybrid"), ("solved-life-path", "hybrid"), ("hot-oh-canada", "whisper")]
NEGATIVE = ("ldn490", "whisper-clean")


def build_transcripts(episodes, store):
    cache_only = WhisperCppTranscriber(cache_dir=CACHE_DIR, runner=lambda *_: (_ for _ in ()).throw(TranscriptionError("cache only")))
    out = {}
    for eid, source in CASES + [NEGATIVE]:
        ep = episodes[eid]
        if source == "hybrid":
            official = load_transcript(store.path(ep.transcripts["official"]))
            dai = load_dai(INTEGRATION_DIR / ep.dai["file"])
            audio = store.path(ep.audio[ep.dai["stitched"]])
            inserts = [cache_only.transcribe(audio, start=r.start, duration=r.duration, language=ep.language) for r in dai.regions]
            out[(eid, source)] = stitched_transcript(official, dai, inserts)
        else:
            out[(eid, source)] = load_transcript(store.path(ep.transcripts["whisper-small"]))
    return out


def make_classifier(spec: str, catalogue, fresh: bool):
    """``model-id`` -> AdClassifier; ``cascade:screen-id>verify-id`` -> CascadeClassifier.
    Returns ``(classifier, cost_fn, description)`` where ``cost_fn(analysis)`` prices the run."""
    if spec.startswith("cascade:"):
        screens_part, verify_id = spec[len("cascade:"):].split(">", 1)
        screen_ids = screens_part.split("+")
        screen_cfgs = [config_for_model(sid, catalogue) for sid in screen_ids]
        verify_cfg = config_for_model(verify_id, catalogue)
        cascade = CascadeConfig(screen=screen_cfgs[0], verify=verify_cfg, extra_screens=tuple(screen_cfgs[1:]))
        scs = [CachedCompletion(OpenAICompatibleClient(c).complete, c.label, fresh=fresh, fingerprint=c.fingerprint()) for c in screen_cfgs]
        vc = CachedCompletion(OpenAICompatibleClient(verify_cfg).complete, verify_cfg.label, fresh=fresh, fingerprint=verify_cfg.fingerprint())

        def factory():
            return CascadeClassifier(cascade, screen_complete=scs[0], verify_complete=vc, extra_screen_completes=scs[1:])

        def cost_fn(analysis, clf):
            # each screen's tokens at its own price (output priced with the verifier, an overestimate)
            total = 0.0
            for sid, toks in zip(screen_ids, clf.screen_tokens_by_model):
                c = catalogue.cost(sid, toks, 0)
                if c is None:
                    return None
                total += c
            v_cost = catalogue.cost(verify_id, clf.verify_tokens, analysis.completion_tokens)
            return None if v_cost is None else total + v_cost

        desc = f"screen {' + '.join(screen_ids)} -> verify {verify_id}"
        return factory, cost_fn, desc, screen_cfgs[0]
    cfg = config_for_model(spec, catalogue)
    completion = CachedCompletion(OpenAICompatibleClient(cfg).complete, cfg.label, fresh=fresh, fingerprint=cfg.fingerprint())

    def factory():
        return AdClassifier(cfg, complete=completion)

    def cost_fn(analysis, clf):
        return catalogue.cost(spec, analysis.prompt_tokens, analysis.completion_tokens)

    return factory, cost_fn, spec, cfg


def run_case(clf, transcript, label, gold, cost_fn):
    t0 = time.monotonic()
    analysis = clf.classify(transcript)
    elapsed = time.monotonic() - t0
    cuts = analysis.cut_intervals(POLICY, min_confidence=MIN_CONFIDENCE)
    ev = evaluate(cuts, gold + dont_care_from_transcript(transcript), duration=label["episode"]["duration_seconds"],
                  policy_categories=set(POLICIES[POLICY]), edge_tolerance=EDGE_TOLERANCE,
                  coverable=[(c.start, c.end) for c in transcript.cues])
    problems = []
    if analysis.degraded:
        problems.append("degraded reply")
    if ev.false_cut_outside_tolerance_seconds > 0:
        problems.append(f"content cut {ev.false_cut_outside_tolerance_seconds:.1f}s")
    if ev.false_cut_seconds > MAX_FALSE_CUT_PER_SEGMENT * max(1, len(ev.segments)):
        problems.append(f"slop {ev.false_cut_seconds:.1f}s")
    if ev.segments_found < len(ev.segments):
        problems.append(f"found {ev.segments_found}/{len(ev.segments)}")
    if ev.coverable_recall < RECALL_FLOOR:
        problems.append(f"recall {ev.coverable_recall:.0%}")
    return {
        "found": f"{ev.segments_found}/{len(ev.segments)}", "coverable_recall": round(ev.coverable_recall, 3),
        "recall": round(ev.recall, 3), "false_cut": round(ev.false_cut_seconds, 1),
        "false_cut_outside": round(ev.false_cut_outside_tolerance_seconds, 1), "combined": round(ev.combined_score, 1),
        "calls": analysis.calls, "prompt_tokens": analysis.prompt_tokens, "completion_tokens": analysis.completion_tokens,
        "reasoning_tokens": analysis.reasoning_tokens, "degraded": analysis.degraded, "warnings": analysis.warnings,
        "cost_usd": cost_fn(analysis, clf),
        "seconds": round(elapsed, 1), "problems": problems, "summary": ev.summary(),
        "reported": [f"{s.start:.0f}-{s.end:.0f} {s.category} conf={s.confidence} cut={analysis.is_cut(s, POLICY, MIN_CONFIDENCE)}"
                     for s in analysis.segments],
    }


def run_negative(clf, transcript, cost_fn):
    t0 = time.monotonic()
    analysis = clf.classify(transcript)
    cuts = analysis.cut_intervals(POLICY, min_confidence=MIN_CONFIDENCE)
    cut_seconds = sum(b - a for a, b in cuts)
    return {
        "cut_seconds": round(cut_seconds, 1), "segments_reported": len(analysis.segments), "calls": analysis.calls,
        "prompt_tokens": analysis.prompt_tokens, "completion_tokens": analysis.completion_tokens,
        "cost_usd": cost_fn(analysis, clf),
        "seconds": round(time.monotonic() - t0, 1), "degraded": analysis.degraded,
        "problems": ([f"cut {cut_seconds:.1f}s from an ad-free episode"] if cuts else []) + (["degraded reply"] if analysis.degraded else []),
        "reported": [f"{s.start:.0f}-{s.end:.0f} {s.category} {s.confidence}" for s in analysis.segments],
    }


def with_retries(fn, *, attempts=4):
    delay = 20.0
    for i in range(attempts):
        try:
            return fn()
        except LLMError as exc:
            if exc.kind in ("rate_limit", "connection", "status") and i < attempts - 1:
                print(f"      {exc.kind}: retrying in {delay:.0f}s ({exc})"[:160], flush=True)
                time.sleep(delay)
                delay *= 2
                continue
            raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--model", action="append", dest="models", help="model id (repeatable)")
    ap.add_argument("--fresh", action="store_true", help="ignore cached replies")
    args = ap.parse_args()
    models = args.models or DEFAULT_MODELS

    episodes = load_manifest()
    store = FixtureStore(allow_download=False)
    catalogue = load_catalogue()
    transcripts = build_transcripts(episodes, store)
    labels = {}
    for eid, source in CASES:
        ep = episodes[eid]
        label = load_label(INTEGRATION_DIR / ep.label)
        labels[eid] = (label, gold_ads(label, allow_provisional=True, audio_sha256=ep.audio[label["episode"]["variant"]].sha256))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results = {}
    for model in models:
        factory, cost_fn, desc, cfg = make_classifier(model, catalogue, args.fresh)
        info = catalogue.get(cfg.model)
        print(f"\n=== {desc}  (screen/model in ${(info.prompt_price if info else 0)*1e6:.3f}/M out ${(info.completion_price if info else 0)*1e6:.3f}/M, "
              f"ctx {info.context_length if info else '?'}, chunk_tokens {cfg.chunk_tokens}, max_tokens {cfg.max_tokens})", flush=True)
        per_model = {"model": model, "pricing": None if info is None else {"prompt_per_m": info.prompt_price * 1e6, "completion_per_m": info.completion_price * 1e6, "context": info.context_length, "free": info.free}, "cases": {}, "error": None}
        try:
            for eid, source in CASES:
                clf = factory()
                label, gold = labels[eid]
                r = with_retries(lambda: run_case(clf, transcripts[(eid, source)], label, gold, cost_fn))
                per_model["cases"][f"{eid}:{source}"] = r
                cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']*100:.2f}c"
                print(f"   {eid:<18} found {r['found']:<5} recall(cued) {r['coverable_recall']:.0%}  false cut {r['false_cut']:>5.1f}s "
                      f"(outside {r['false_cut_outside']:.1f}s)  calls {r['calls']}  {r['prompt_tokens']:>7} tok  {cost:>7}  {r['seconds']:>5.1f}s  "
                      f"{'OK' if not r['problems'] else 'FAIL: ' + '; '.join(r['problems'])}", flush=True)
            clf = factory()
            r = with_retries(lambda: run_negative(clf, transcripts[NEGATIVE], cost_fn))
            per_model["cases"]["ldn490:whisper-clean-negative"] = r
            cost = "n/a" if r["cost_usd"] is None else f"{r['cost_usd']*100:.2f}c"
            print(f"   {'negative':<18} cut {r['cut_seconds']}s reported {r['segments_reported']}  calls {r['calls']}  {r['prompt_tokens']:>7} tok  {cost:>7}  "
                  f"{'OK' if not r['problems'] else 'FAIL: ' + '; '.join(r['problems'])}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one model's failure must not stop the comparison
            per_model["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            print(f"   ERROR {per_model['error']}", flush=True)
        results[model] = per_model

    # ---- table
    lines = ["| Model | $/M in | Cost, 5 cases | Cost per LdN-size episode | Verdict | Details |", "|---|---|---|---|---|---|"]
    for model, res in results.items():
        cases = res["cases"]
        costs = [c["cost_usd"] for c in cases.values() if c.get("cost_usd") is not None]
        total = sum(costs) if costs else None
        ldn = cases.get("ldn491:hybrid", {}).get("cost_usd")
        problems = [f"{k.split(':')[0]}: {'; '.join(v['problems'])}" for k, v in cases.items() if v.get("problems")]
        verdict = "ERROR" if res["error"] else ("PASS" if not problems and len(cases) == 5 else "FAIL")
        pin = res["pricing"]["prompt_per_m"] if res["pricing"] else float("nan")
        lines.append(f"| {model} | {pin:.3f} | {('%.2f cent' % (total*100)) if total is not None else 'n/a'} | "
                     f"{('%.3f cent' % (ldn*100)) if ldn is not None else 'n/a'} | {verdict} | {res['error'] or '; '.join(problems) or 'all gates met'} |")
    table = "\n".join(lines)
    print("\n" + table)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"model-comparison-{stamp}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str))
    (REPORTS_DIR / f"model-comparison-{stamp}.md").write_text(table + "\n")
    print(f"\nwrote {REPORTS_DIR / f'model-comparison-{stamp}.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
