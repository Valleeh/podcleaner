"""Two-stage ad classification: a cheap high-recall screen, then a precise verifier.

Measured 2026-09-06 on the integration episodes: models cheap enough for a whole
transcript at well under a cent (0.1-0.4 cent per 90-minute episode) find most ads but
miss some that blend into the programme and misplace boundaries; models that place
boundaries reliably cost 5-15 cents for the whole transcript.  The cascade spends the
cheap model on everything and the precise model only on the few minutes around each
candidate, so the per-episode cost is dominated by the cheap pass while the decision
that touches audio is made by the better model.

Stage 1 (screen) uses the normal prompt plus an instruction to err on the side of
reporting, and keeps *every* reported segment regardless of category or confidence.
Stage 2 (verify) renders the cues from ``context_seconds`` before each candidate cluster
to ``context_seconds`` after it, with their global indices, and asks the verifier the
normal question.  The verifier's answer is the result: what it does not confirm is not
cut, and it may also correct the candidate's edges or split a stacked break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from podcleaner.detect.llm import (
    AdAnalysis,
    AdClassifier,
    AdSegment,
    CompleteFn,
    LLMConfig,
    OpenAICompatibleClient,
    build_system_prompt,
    merge_segments,
    parse_model_json,
    render_transcript,
    reply_looks_corrupted,
    resolve_segments,
    shape_warnings,
)
from podcleaner.logging import get_logger
from podcleaner.transcripts import Cue, Transcript, format_clock

__all__ = ["DEFAULT_SPEC", "CascadeClassifier", "CascadeConfig", "candidate_windows", "classifier_from_spec", "parse_spec"]

logger = get_logger(__name__)

SCREEN_SUFFIX = """

This is a screening pass. Another, more careful reviewer will check every segment you \
report and fix its boundaries, so err on the side of reporting: include anything that \
might be promotional, even at low confidence, and never leave out a candidate because \
you are unsure. Missing a segment here cannot be repaired later; a false alarm can."""

VERIFY_PREFIX = """\
(Excerpt of a longer episode, {clock_start}-{clock_end}. A screening pass flagged \
{clock_cand_start}-{clock_cand_end} as possibly promotional; decide for yourself what, \
if anything, in this excerpt is advertising or promotion, and report exact cue ranges. \
Editorial content that merely mentions products is not an ad. Your answer will be cut \
out of the audio, so precision matters more than completeness: when you are unsure \
whether an edge cue belongs to the ad, leave it out; never include the hosts' own \
conversation, the programme's opening or closing announcements, or a cue that merely \
reacts to an ad.)
"""


@dataclass(frozen=True)
class CascadeConfig:
    #: one screening model, or several whose candidates are pooled (recall of the union)
    screen: LLMConfig
    verify: LLMConfig
    extra_screens: Tuple[LLMConfig, ...] = ()
    #: seconds of transcript rendered before and after a candidate cluster for the verifier
    context_seconds: float = 90.0
    #: candidates closer than this are verified together
    cluster_gap_seconds: float = 45.0
    #: a screening segment longer than this is rendered in pieces by the verifier
    max_window_seconds: float = 900.0

    @property
    def screens(self) -> Tuple[LLMConfig, ...]:
        return (self.screen,) + tuple(self.extra_screens)

    @property
    def label(self) -> str:
        return f"cascade({' + '.join(c.label for c in self.screens)} -> {self.verify.label})"

    def fingerprint(self) -> str:
        return "cascade|" + "|".join(c.fingerprint() for c in self.screens) + f"|{self.verify.fingerprint()}|{self.context_seconds}|{self.cluster_gap_seconds}"


def candidate_windows(segments: Sequence[AdSegment], *, gap: float) -> List[Tuple[float, float]]:
    """Merge candidate intervals that lie within ``gap`` seconds of each other."""
    out: List[Tuple[float, float]] = []
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        if out and seg.start <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], seg.end))
        else:
            out.append((seg.start, seg.end))
    return out


class CascadeClassifier:
    """Screen with ``config.screen``, verify candidates with ``config.verify``."""

    def __init__(
        self,
        config: CascadeConfig,
        *,
        screen_complete: Optional[CompleteFn] = None,
        verify_complete: Optional[CompleteFn] = None,
        extra_screen_completes: Sequence[Optional[CompleteFn]] = (),
    ) -> None:
        self.config = config
        completes = [screen_complete] + list(extra_screen_completes)
        self._screens = []
        for i, screen_cfg in enumerate(config.screens):
            complete = completes[i] if i < len(completes) and completes[i] is not None else OpenAICompatibleClient(screen_cfg).complete
            self._screens.append(AdClassifier(screen_cfg, complete=complete))
        self._verify_complete = verify_complete or OpenAICompatibleClient(config.verify).complete
        self.calls = 0
        self.screen_tokens = 0
        #: prompt tokens per screening model, in config order
        self.screen_tokens_by_model: List[int] = [0 for _ in config.screens]
        self.verify_tokens = 0

    def classify(self, transcript: Transcript) -> AdAnalysis:
        cfg = self.config
        analysis = AdAnalysis(segments=[], model=cfg.label, cue_count=len(transcript.cues), duration=transcript.end)
        # ---- stage 1: screen the whole transcript with every screening model, pool everything reported
        pooled: List[AdSegment] = []
        for i, screen in enumerate(self._screens):
            screen.system_prompt_suffix = SCREEN_SUFFIX
            screened = screen.classify(transcript)
            self.calls += screened.calls
            self.screen_tokens += screened.prompt_tokens
            self.screen_tokens_by_model[i] += screened.prompt_tokens
            analysis.calls += screened.calls
            analysis.chunks = max(analysis.chunks, screened.chunks)
            analysis.prompt_tokens += screened.prompt_tokens
            analysis.completion_tokens += screened.completion_tokens
            analysis.reasoning_tokens += screened.reasoning_tokens
            analysis.warnings.extend(f"screen[{screen.config.model}]: {w}" for w in screened.warnings)
            analysis.degraded = analysis.degraded or screened.degraded
            analysis.raw_replies.extend(screened.raw_replies)
            pooled.extend(screened.segments)
            analysis.warnings.append(f"screen[{screen.config.model}] reported {len(screened.segments)} segment(s)")
        candidates = candidate_windows(pooled, gap=cfg.cluster_gap_seconds)
        analysis.warnings.append(f"{len(pooled)} candidate segment(s) in {len(candidates)} window(s)")
        if not candidates:
            return analysis

        # ---- stage 2: verify each window with context
        system_prompt = build_system_prompt()
        verified: List[AdSegment] = []
        cues = transcript.cues
        for n, (c_start, c_end) in enumerate(candidates):
            w_start, w_end = c_start - cfg.context_seconds, c_end + cfg.context_seconds
            window = [c for c in cues if c.end > w_start and c.start < w_end]
            if not window:
                continue
            if window[-1].end - window[0].start > cfg.max_window_seconds + 2 * cfg.context_seconds:
                analysis.warnings.append(
                    f"verify: window {n + 1} ({format_clock(c_start)}-{format_clock(c_end)}) is "
                    f"{c_end - c_start:.0f}s long; verifying it whole")
            user_prompt = VERIFY_PREFIX.format(
                clock_start=format_clock(window[0].start), clock_end=format_clock(window[-1].end),
                clock_cand_start=format_clock(c_start), clock_cand_end=format_clock(c_end),
            ) + render_transcript(window)
            logger.info("cascade_verify", window=n + 1, windows=len(candidates), cues=len(window),
                        candidate=[round(c_start, 1), round(c_end, 1)])
            raw = None
            for attempt in range(2):
                prompt = user_prompt if attempt == 0 else user_prompt + "\n\n(Retry: the previous reply was malformed. Respond with the JSON object only.)"
                reply, usage = self._verify_complete(system_prompt, prompt)
                self.calls += 1
                analysis.calls += 1
                analysis.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                analysis.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
                analysis.reasoning_tokens += int(usage.get("reasoning_tokens", 0) or 0)
                self.verify_tokens += int(usage.get("prompt_tokens", 0) or 0)
                analysis.raw_replies.append(reply)
                candidate = parse_model_json(reply)
                problem = reply_looks_corrupted(candidate)
                if problem is None:
                    raw = candidate
                    break
                analysis.warnings.append(f"verify: window {n + 1} reply {attempt + 1} corrupted ({problem})")
                raw = candidate
                if attempt == 1:
                    analysis.degraded = True
            segments, warnings = resolve_segments(raw, window, chunk=1000 + n)
            analysis.warnings.extend(f"verify window {n + 1}: {w}" for w in warnings)
            verified.extend(segments)
        merged, warnings = merge_segments(verified)
        analysis.segments = merged
        analysis.warnings.extend(warnings)
        analysis.warnings.extend(shape_warnings(analysis, "promos"))
        return analysis


# --------------------------------------------------------------------------------------
# spec strings: "model-id" or "cascade:screen[+screen2]>verifier"
# --------------------------------------------------------------------------------------


def parse_spec(spec: str) -> Tuple[List[str], Optional[str]]:
    """``(screen_ids, verify_id)``; ``verify_id`` is ``None`` for a single model."""
    spec = spec.strip()
    if spec.startswith("cascade:"):
        screens, verify = spec[len("cascade:"):].split(">", 1)
        ids = [x.strip() for x in screens.split("+") if x.strip()]
        if not ids or not verify.strip():
            raise ValueError(f"bad cascade spec {spec!r}; expected cascade:screen[+screen]>verifier")
        return ids, verify.strip()
    if not spec:
        raise ValueError("empty model spec")
    return [spec], None


#: The production default, chosen 2026-09-06 on the integration episodes: the cheapest
#: configuration that met every gate.  0.23 cent per 90-minute episode, 0.53 cent for a
#: four-hour one.  ``cascade:qwen/qwen3.7-flash>google/gemini-2.5-flash`` (0.67 cent) also
#: passed; ``anthropic/claude-sonnet-5`` alone passed at 13 cent.  See
#: docs/integration-tests.md, section 6.
DEFAULT_SPEC = "cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash"


def classifier_from_spec(spec: Optional[str] = None, *, base_config: Optional[LLMConfig] = None):
    """Build the production classifier for ``spec`` (default: ``$PODCLEANER_LLM_SPEC``,
    else :data:`DEFAULT_SPEC`).  API key, base URL and other settings come from
    ``base_config`` or the environment.  A bare model id gives a single-model classifier."""
    import os

    spec = spec or os.environ.get("PODCLEANER_LLM_SPEC") or DEFAULT_SPEC
    base = base_config or LLMConfig.from_env()
    screen_ids, verify_id = parse_spec(spec)
    make = lambda model: LLMConfig(**{**base.__dict__, "model": model})  # noqa: E731
    if verify_id is None:
        return AdClassifier(make(screen_ids[0]))
    cfg = CascadeConfig(screen=make(screen_ids[0]), verify=make(verify_id), extra_screens=tuple(make(m) for m in screen_ids[1:]))
    return CascadeClassifier(cfg)
