"""Shared helpers for the integration tests (kept out of conftest so tests can import them)."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

from podcleaner.detect.llm import LLMConfig
from podcleaner.eval.dai import DaiResult, InsertedRegion
from podcleaner.eval.fixtures import Fixture, FixtureError, FixtureStore, REPO_ROOT
from podcleaner.eval.pricing import ModelCatalogue
from podcleaner.transcripts import Cue, Transcript

INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
BASELINES_PATH = INTEGRATION_DIR / "baselines.json"
REPORTS_DIR = REPO_ROOT / "var" / "reports"
CACHE_DIR = REPO_ROOT / "var" / "cache"


def require(store: FixtureStore, fixture: Fixture) -> Path:
    """Resolve a fixture or skip the test with the store's explanation."""
    try:
        return store.resolve(fixture)
    except FixtureError as exc:
        pytest.skip(f"fixture unavailable ({exc.kind}): {exc}")


def load_dai(path: Path) -> DaiResult:
    d = json.loads(path.read_text())
    regions = [
        InsertedRegion(r["start"], r["end"], r["start_frame"], r["end_frame"], r["byte_start"], r["byte_end"],
                       r.get("skipped_clean_frames", 0), r.get("skipped_clean_seconds", 0.0))
        for r in d["regions"]
    ]
    res = DaiResult(regions, d["clean_duration"], d["stitched_duration"], d["clean_frames"], d["stitched_frames"],
                    d["matched_frames"], d.get("modified_frames", 0), d.get("clean_path"), d.get("stitched_path"))
    shift = 0.0
    for r in regions:
        res.offset_map.append((r.start - shift, shift + r.duration))
        shift += r.duration
    return res


def stitched_transcript(official: Transcript, dai: DaiResult, inserts: List[Transcript]) -> Transcript:
    """A transcript of the *stitched* file assembled from the publisher transcript of the
    clean master (timestamps mapped through the DAI offset map) plus whisper transcripts
    of the inserted regions.  Editorial text is publisher quality; ad text is whisper's."""
    cues: List[Cue] = []
    for c in official.cues:
        # A cue that straddles a splice would otherwise be stretched across the whole
        # insert (its end maps past the inserted audio).  Split it: the words stay with
        # the part before the ad, and a continuation marker resumes after it.
        splice_points = [
            (r, at) for r, (at, _shift) in zip(dai.regions, dai.offset_map) if c.start < at < c.end
        ]
        if splice_points:
            region, at = splice_points[0]
            cues.append(Cue(0, dai.to_stitched(c.start), region.start, c.text, c.speaker))
            cues.append(Cue(0, region.end, dai.to_stitched(c.end), "…", c.speaker))
        else:
            cues.append(Cue(0, dai.to_stitched(c.start), dai.to_stitched(c.end), c.text, c.speaker))
    for t in inserts:
        for c in t.cues:
            if c.text.strip():
                cues.append(Cue(0, c.start, c.end, c.text))
    cues.sort(key=lambda c: (c.start, c.end))
    cues = [Cue(i + 1, c.start, c.end, c.text, c.speaker) for i, c in enumerate(cues)]
    continuation = [(c.start, c.end) for c in cues if c.text == "…"]
    return Transcript(cues, language=official.language, engine="hybrid(official+whisper-inserts)",
                      duration=dai.stitched_duration,
                      meta={"inserted_regions": dai.intervals(), "continuation_cues": continuation})


def dont_care_from_transcript(transcript: Transcript):
    """Ambiguous gold spans for the continuation markers a hybrid transcript carries: the
    tail of a host sentence interrupted by an insert has no words in the transcript, so
    cutting or keeping it is not a judgement about content."""
    from podcleaner.eval.adscore import GoldAd

    return [GoldAd(a, b, "other", ambiguous=True, note="continuation of an interrupted cue")
            for a, b in transcript.meta.get("continuation_cues", []) if b > a]


def synth_wav(path: Path, seconds: float, *, noise_amplitude: int = 0, seed: int = 7, rate: int = 16000) -> Path:
    """Write a mono 16-bit WAV of silence, or of low-level white noise, with the stdlib."""
    import random

    rng = random.Random(seed)
    n = int(seconds * rate)
    if noise_amplitude:
        frames = b"".join(struct.pack("<h", rng.randint(-noise_amplitude, noise_amplitude)) for _ in range(n))
    else:
        frames = b"\x00\x00" * n
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


# --------------------------------------------------------------------------------------
# LLM reply cache (so re-running assertions does not re-bill; --llm-fresh bypasses)
# --------------------------------------------------------------------------------------


class CachedCompletion:
    def __init__(self, inner: Callable[[str, str], Tuple[str, dict]], model: str, *, fresh: bool = False,
                 fingerprint: str = "") -> None:
        self.inner = inner
        self.model = model
        self.fingerprint = fingerprint or model
        self.fresh = fresh
        self.dir = CACHE_DIR / "llm"
        self.hits = 0
        self.misses = 0

    def __call__(self, system_prompt: str, user_prompt: str) -> Tuple[str, dict]:
        key = hashlib.sha256((self.fingerprint + "\x00" + system_prompt + "\x00" + user_prompt).encode()).hexdigest()[:24]
        path = self.dir / f"{key}.json"
        if path.exists() and not self.fresh:
            d = json.loads(path.read_text())
            self.hits += 1
            return d["reply"], {**d.get("usage", {}), "cached": True}
        reply, usage = self.inner(system_prompt, user_prompt)
        self.misses += 1
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": self.model, "fingerprint": self.fingerprint, "reply": reply, "usage": usage,
                                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "prompt_sha256": key}, ensure_ascii=False, indent=1))
        return reply, usage


# --------------------------------------------------------------------------------------
# baselines and reports
# --------------------------------------------------------------------------------------


@dataclass
class Baselines:
    data: Dict[str, dict] = field(default_factory=dict)
    record: bool = False
    updated: Dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, record: bool) -> "Baselines":
        data = json.loads(BASELINES_PATH.read_text()) if BASELINES_PATH.exists() else {}
        return cls(data=data.get("metrics", {}), record=record)

    def get(self, key: str) -> Optional[dict]:
        return self.data.get(key)

    def observe(self, key: str, metrics: dict) -> None:
        if self.record:
            self.updated[key] = {**metrics, "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def save(self) -> None:
        if not self.updated:
            return
        merged = {**self.data, **self.updated}
        BASELINES_PATH.write_text(json.dumps({
            "_comment": "Measured on the pinned fixtures; regression gates are derived from these values. "
                        "Re-record with: pytest --integration --record-baselines",
            "metrics": dict(sorted(merged.items())),
        }, indent=2) + "\n")


def gate(baseline: Optional[dict], metric: str, measured: float, *, margin: float, lower_is_better: bool = True) -> Optional[str]:
    """Regression check against a recorded baseline; returns a failure message or None."""
    if not baseline or metric not in baseline:
        return None
    ref = baseline[metric]
    if lower_is_better and measured > ref + margin:
        return f"{metric} regressed: {measured:.3f} vs baseline {ref:.3f} (+{margin} allowed)"
    if not lower_is_better and measured < ref - margin:
        return f"{metric} regressed: {measured:.3f} vs baseline {ref:.3f} (-{margin} allowed)"
    return None


@dataclass
class Report:
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    entries: List[dict] = field(default_factory=list)

    def add(self, kind: str, key: str, **metrics) -> None:
        self.entries.append({"kind": kind, "key": key, **metrics})

    def save(self) -> Optional[Path]:
        if not self.entries:
            return None
        out = REPORTS_DIR / self.started
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(self.entries, indent=1, ensure_ascii=False, default=str))
        return out / "report.json"

    def summary_lines(self) -> List[str]:
        lines = []
        for e in self.entries:
            metrics = {k: v for k, v in e.items() if k not in ("kind", "key") and not isinstance(v, (list, dict))}
            body = "  ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items())
            lines.append(f"{e['kind']:<12} {e['key']:<40} {body}")
        return lines


# --------------------------------------------------------------------------------------
# model catalogue helpers
# --------------------------------------------------------------------------------------


def load_catalogue(offline: bool = False) -> ModelCatalogue:
    try:
        return ModelCatalogue.load(CACHE_DIR / "openrouter-models.json", offline=offline)
    except Exception:  # noqa: BLE001 - pricing is a nicety, never a reason to fail a test
        return ModelCatalogue({})


def build_classifier(spec: Optional[str], catalogue: ModelCatalogue, *, fresh: bool = False):
    """``None``/model id -> AdClassifier; ``cascade:a[+b]>v`` -> CascadeClassifier.  Replies of
    every stage go through the reply cache.  Returns ``(classifier, primary_config, cost_fn)``
    where ``cost_fn(analysis, classifier)`` prices the run from the catalogue."""
    from podcleaner.detect.cascade import CascadeClassifier, CascadeConfig
    from podcleaner.detect.llm import AdClassifier, OpenAICompatibleClient

    if spec and spec.startswith("cascade:"):
        screens_part, verify_id = spec[len("cascade:"):].split(">", 1)
        screen_ids = screens_part.split("+")
        screen_cfgs = [config_for_model(sid, catalogue) for sid in screen_ids]
        verify_cfg = config_for_model(verify_id, catalogue)
        cascade = CascadeConfig(screen=screen_cfgs[0], verify=verify_cfg, extra_screens=tuple(screen_cfgs[1:]))
        scs = [CachedCompletion(OpenAICompatibleClient(c).complete, c.label, fresh=fresh, fingerprint=c.fingerprint()) for c in screen_cfgs]
        vc = CachedCompletion(OpenAICompatibleClient(verify_cfg).complete, verify_cfg.label, fresh=fresh, fingerprint=verify_cfg.fingerprint())
        clf = CascadeClassifier(cascade, screen_complete=scs[0], verify_complete=vc, extra_screen_completes=scs[1:])

        def cost_fn(analysis, classifier):
            total = 0.0
            for sid, toks in zip(screen_ids, classifier.screen_tokens_by_model):
                c = catalogue.cost(sid, toks, 0)
                if c is None:
                    return None
                total += c
            v = catalogue.cost(verify_id, classifier.verify_tokens, analysis.completion_tokens)
            return None if v is None else total + v

        return clf, screen_cfgs[0], cost_fn
    cfg = config_for_model(spec, catalogue)
    completion = CachedCompletion(OpenAICompatibleClient(cfg).complete, cfg.label, fresh=fresh, fingerprint=cfg.fingerprint())
    clf = AdClassifier(cfg, complete=completion)
    return clf, cfg, (lambda analysis, classifier: catalogue.cost(cfg.model, analysis.prompt_tokens, analysis.completion_tokens))


def config_for_model(model: Optional[str], catalogue: ModelCatalogue, **overrides) -> LLMConfig:
    """LLMConfig for ``model`` with the output budget and chunk size clamped to what the
    catalogue says the model can take."""
    cfg = LLMConfig.from_env(**({"model": model} if model else {}), **overrides)
    info = catalogue.get(cfg.model)
    if info is None:
        return cfg
    max_tokens = cfg.max_tokens
    if info.max_completion_tokens:
        max_tokens = min(max_tokens, int(info.max_completion_tokens))
    chunk_tokens = cfg.chunk_tokens
    if info.context_length:
        chunk_tokens = min(chunk_tokens, max(8000, int(info.context_length * 0.45)))
    return LLMConfig(**{**cfg.__dict__, "max_tokens": max_tokens, "chunk_tokens": chunk_tokens})
