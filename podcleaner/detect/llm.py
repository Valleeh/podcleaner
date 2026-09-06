"""LLM classification of transcript cues into advertising segments.

Port of ``ad_analyzer.py`` from the ``whisper-transcribe`` repo, restructured around
:class:`podcleaner.transcripts.Transcript` and made testable offline.  Two design
decisions carried over unchanged, because they are what make the output safe to cut on:

1. **The model returns cue indices, never timestamps.**  Models miscopy long timestamp
   strings but handle small integers well, and an integer can be range-checked against
   the cues we rendered.  Every timestamp in the output is looked up from our own parse.
2. **The model classifies; local policy decides what to cut.**  The model never sees the
   cut/keep rules, so changing the policy is free and needs no second API call.

Added here:

* **Chunking with overlap** for long transcripts (a four-hour episode renders to well
  over 100k tokens).  Chunks are rendered with *global* cue indices, and segments found
  in overlapping chunks are merged.
* **Injectable completion function**, so the classifier can be exercised with canned
  replies and the number of calls asserted.  ``AdClassifier.calls`` counts real ones.

Backends: any OpenAI-compatible endpoint.  Defaults to OpenRouter with a Claude Sonnet
model (``OPENROUTER_API_KEY``); ``OPENAI_API_KEY`` and ``PODCLEANER_LLM_*`` are also read.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from podcleaner.logging import get_logger
from podcleaner.transcripts import Cue, Transcript, format_clock

__all__ = [
    "CATEGORIES",
    "DEFAULT_MAX_CUT_DURATION",
    "POLICIES",
    "AdAnalysis",
    "AdClassifier",
    "AdSegment",
    "LLMConfig",
    "LLMError",
    "OpenAICompatibleClient",
    "build_system_prompt",
    "chunk_cues",
    "estimate_tokens",
    "merge_segments",
    "parse_model_json",
    "render_transcript",
    "reply_looks_corrupted",
    "resolve_segments",
]

logger = get_logger(__name__)

Interval = Tuple[float, float]


class LLMError(RuntimeError):
    """A backend problem, with a ``kind`` for callers that want to branch on it."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------------------
# categories, policies, prompt
# --------------------------------------------------------------------------------------

CATEGORIES: Dict[str, str] = {
    "sponsor_read": "A paid advertisement for a third-party product or service, read by "
    "the host or played as a produced spot (including ads inserted by the podcast host's "
    "ad server, which may use a different voice and start or end abruptly).",
    "host_endorsement": "A host personally vouching for a sponsor, often woven into "
    "conversation rather than read as a scripted spot.",
    "cross_promo": "A trailer or plug for a different show, usually from the same "
    "network. Frequently stacked before the episode actually begins.",
    "self_promo": "Promotion of this same show: subscribe/rate-and-review asks, live "
    "events, merchandise, bonus feeds, paid membership tiers.",
    "credits": "Production credits and closing network boilerplate.",
}

POLICIES: Dict[str, frozenset] = {
    # Default: paid ads and promos for other shows are cut; the episode's own
    # subscribe asks and closing credits stay.
    "promos": frozenset({"sponsor_read", "host_endorsement", "cross_promo"}),
    "sponsors": frozenset({"sponsor_read", "host_endorsement"}),
    "all": frozenset(CATEGORIES),
}

#: Longest segment that is ever cut automatically.  Longer ones are reported only.
DEFAULT_MAX_CUT_DURATION: float = 600.0

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_cue": {"type": "integer"},
                    "end_cue": {"type": "integer"},
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["start_cue", "end_cue", "category", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are an expert audio editor who locates advertising and promotional blocks \
in podcast transcripts so they can be cut out. Transcripts may be in English or \
German (German ad breaks often open with "Werbung", "Anzeige", "präsentiert von" \
or "unterstützt von" and close with "und jetzt zurück zur Sendung" or a similar phrase).

You will receive a numbered transcript, possibly an excerpt of a longer episode. \
Each line is:

    [<cue number>] <timestamp> <spoken text>

Identify every contiguous run of cues that is NOT the episode's editorial \
content. For each run, report the first and last cue number belonging to it.

Categories:
{categories}

Rules that matter:

- Report cue NUMBERS only, exactly as printed. Never write timestamps; they are \
looked up locally.
- Boundaries must be tight. The first cue of a segment is the first cue whose \
spoken words belong to the ad, and the last is the last such cue. Do not \
include the host's editorial sentence that leads into or out of the break.
- Podcasts commonly stack several unrelated ads back to back. Report each as \
its own segment rather than merging them, even when they are adjacent.
- A mid-roll often begins with a hand-off phrase ("we'll be right back", \
"a quick word from our sponsors") and ends with a return phrase ("we're back", \
"let's get back into it"). Those framing lines are part of the break.
- Ads inserted by an ad server can appear anywhere, even mid-sentence, in a \
different voice, and the transcript may contain broken half-sentences at the \
seams. The inserted material is the ad; the host's interrupted sentence is not.
- Editorial discussion that merely mentions a company or product is NOT an ad. \
An ad is trying to sell the listener something.
- Confidence is your own 0.0-1.0 estimate that the segment is genuinely \
promotional and the boundaries are right. Be honest; low values are useful.
- If there are no promotional segments at all, return an empty list.
- Keep each reason under 15 words. Report promotional runs only, never the \
episode's own content, so a typical episode has fewer than 20 segments.

Respond with JSON only, in exactly this shape:

{{"segments": [
  {{"start_cue": <int>, "end_cue": <int>, "category": "<one of the categories>",
    "confidence": <float 0-1>, "reason": "<short justification>"}}
]}}
"""


def build_system_prompt() -> str:
    cats = "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())
    return SYSTEM_PROMPT.format(categories=cats)


def render_transcript(cues: Sequence[Cue]) -> str:
    return "\n".join(f"[{c.index}] {format_clock(c.start)} {c.text}" for c in cues)


#: Calibrated against llama.cpp's count for a full-episode prompt: 84285 characters came
#: to 32181 tokens.  Cue lines are mostly digits and brackets, which tokenise poorly.
CHARS_PER_TOKEN = 2.6


def estimate_tokens(text: str) -> int:
    """Order-of-magnitude token estimate; only the backend knows for certain."""
    return int(len(text) / CHARS_PER_TOKEN)


# --------------------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------------------


def chunk_cues(cues: Sequence[Cue], *, max_tokens: int, overlap_cues: int) -> List[List[Cue]]:
    """Split ``cues`` into contiguous chunks whose rendered size stays under
    ``max_tokens`` (estimated).  Each chunk after the first starts ``overlap_cues``
    before the previous one ended, so a segment straddling a boundary is complete in at
    least one chunk."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    if overlap_cues < 0:
        raise ValueError("overlap_cues must be >= 0")
    cues = list(cues)
    if not cues:
        return []
    sizes = [estimate_tokens(f"[{c.index}] {format_clock(c.start)} {c.text}\n") for c in cues]
    if sum(sizes) <= max_tokens:
        return [cues]
    chunks: List[List[Cue]] = []
    start = 0
    n = len(cues)
    while start < n:
        total = 0
        end = start
        while end < n and (total + sizes[end] <= max_tokens or end == start):
            total += sizes[end]
            end += 1
        chunks.append(cues[start:end])
        if end >= n:
            break
        start = max(end - overlap_cues, start + 1)
    return chunks


# --------------------------------------------------------------------------------------
# parsing and validation of the model's reply
# --------------------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_model_json(raw: str) -> dict:
    """Parse the reply as JSON, tolerating a markdown fence and surrounding prose."""
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1)
    elif raw.lstrip().startswith("```"):
        raw = raw.lstrip()[3:]
        raw = raw[4:] if raw.startswith("json") else raw
    excerpt = f"{raw[:300]!r} ... {raw[-200:]!r}" if len(raw) > 500 else repr(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"could not parse model reply as JSON: {excerpt}", kind="bad_reply")
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"could not parse model reply as JSON ({exc.msg} at {exc.pos} of {len(raw)}): {excerpt}", kind="bad_reply") from exc
    if not isinstance(data, dict):
        raise LLMError(f"model reply is not a JSON object: {raw[:200]!r}", kind="bad_reply")
    return data


@dataclass
class AdSegment:
    start_cue: int
    end_cue: int
    start: float
    end: float
    category: str
    confidence: float
    reason: str = ""
    chunk: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def interval(self) -> Interval:
        return (self.start, self.end)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["start"] = round(self.start, 3)
        d["end"] = round(self.end, 3)
        d["duration"] = round(self.duration, 3)
        return d


def resolve_segments(raw: dict, cues: Sequence[Cue], *, chunk: int = 0) -> Tuple[List[AdSegment], List[str]]:
    """Turn the model's cue-index output into timestamped segments.

    Structural mistakes are repaired or dropped *here*, with a warning each, rather
    than passed on to code that deletes audio.  Indices are the cues' own ``index``
    values (global to the transcript), which is what the model saw.
    """
    warnings: List[str] = []
    by_index = {c.index: c for c in cues}
    if not by_index:
        raise LLMError("no cues to resolve against", kind="bad_input")
    lo, hi = min(by_index), max(by_index)
    items = raw.get("segments")
    if not isinstance(items, list):
        raise LLMError(f"model reply has no 'segments' list: {raw!r}"[:500], kind="bad_reply")

    out: List[AdSegment] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            warnings.append(f"segment {i}: not an object, dropped")
            continue
        try:
            start_cue = int(item["start_cue"])
            end_cue = int(item["end_cue"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"segment {i}: missing/invalid cue numbers, dropped")
            continue
        if start_cue > end_cue:
            warnings.append(f"segment {i}: start_cue {start_cue} > end_cue {end_cue}, swapped")
            start_cue, end_cue = end_cue, start_cue
        if start_cue not in by_index or end_cue not in by_index:
            # An index we never rendered is not a small slip: clamping it (as the v1
            # analyzer did) can turn one garbage number into a cut spanning the whole
            # episode.  Content safety says drop it and say so.
            warnings.append(
                f"segment {i}: cue range {start_cue}-{end_cue} not within the rendered "
                f"cues {lo}-{hi}, dropped"
            )
            continue
        category = str(item.get("category", "")).strip()
        if category not in CATEGORIES:
            warnings.append(f"segment {i}: unknown category {category!r}, treated as sponsor_read")
            category = "sponsor_read"
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence != confidence:  # NaN
            confidence = 0.0
        if not 0.0 <= confidence <= 1.0:
            warnings.append(f"segment {i}: confidence {confidence} outside 0-1, clamped")
            confidence = min(1.0, max(0.0, confidence))
        out.append(
            AdSegment(
                start_cue=start_cue,
                end_cue=end_cue,
                start=by_index[start_cue].start,
                end=by_index[end_cue].end,
                category=category,
                confidence=confidence,
                reason=str(item.get("reason", "")).strip(),
                chunk=chunk,
            )
        )
    return out, warnings


def merge_segments(
    segments: Iterable[AdSegment],
    *,
    touch: float = 0.0,
    max_merge_duration: float = DEFAULT_MAX_CUT_DURATION,
) -> Tuple[List[AdSegment], List[str]]:
    """Merge segments that overlap in time (or come within ``touch`` seconds).

    Overlaps arise from chunk overlap or from a sloppy model; either way the cutter
    must never see two segments covering the same audio.  Adjacent *distinct* ads are
    deliberately kept apart (``touch`` defaults to 0), because merging first would fuse
    two correct segments into one interval with the wrong edges.

    A segment longer than ``max_merge_duration`` is implausible as an advertisement and
    is passed through untouched with a warning, so that it can neither swallow a correct
    neighbour nor be cut (see :meth:`AdAnalysis.cut_intervals`).
    """
    warnings: List[str] = []
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    merged: List[AdSegment] = []
    implausible: List[AdSegment] = []
    for seg in ordered:
        if seg.duration > max_merge_duration:
            warnings.append(
                f"segment {format_clock(seg.start)}-{format_clock(seg.end)} is {seg.duration:.0f}s long, "
                f"longer than any plausible advertisement ({max_merge_duration:.0f}s); reported, never cut"
            )
            implausible.append(dataclasses.replace(seg))
            continue
        if merged and seg.start < merged[-1].end + touch - 1e-9:
            prev = merged[-1]
            if seg.chunk != prev.chunk and seg.start >= prev.start and seg.end <= prev.end + 1e-9:
                # the same segment seen from an overlapping chunk: silent
                prev.confidence = max(prev.confidence, seg.confidence)
                continue
            warnings.append(
                f"segments overlap ({format_clock(prev.start)}-{format_clock(prev.end)} and "
                f"{format_clock(seg.start)}-{format_clock(seg.end)}), merged"
            )
            prev.end = max(prev.end, seg.end)
            prev.end_cue = max(prev.end_cue, seg.end_cue)
            prev.confidence = min(prev.confidence, seg.confidence)
            if seg.category != prev.category:
                prev.reason = f"{prev.reason} + {seg.reason}".strip(" +")
        else:
            merged.append(dataclasses.replace(seg))
    merged.extend(implausible)
    merged.sort(key=lambda s: (s.start, s.end))
    return merged, warnings


# --------------------------------------------------------------------------------------
# result
# --------------------------------------------------------------------------------------


@dataclass
class AdAnalysis:
    segments: List[AdSegment]
    warnings: List[str] = field(default_factory=list)
    model: str = ""
    calls: int = 0
    chunks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    raw_replies: List[str] = field(default_factory=list)
    cue_count: int = 0
    duration: float = 0.0
    #: True when some chunk's reply stayed corrupted after retrying; segments may be missing
    degraded: bool = False

    def cut_intervals(
        self,
        policy: str = "promos",
        *,
        min_confidence: float = 0.5,
        max_cut_duration: float = DEFAULT_MAX_CUT_DURATION,
    ) -> List[Interval]:
        """Intervals to cut under ``policy``.

        A segment is reported but *not* cut when its confidence is below
        ``min_confidence`` or it is longer than ``max_cut_duration`` -- no advertisement
        runs for ten minutes, but a feed drop of a whole other episode does, and cutting
        that would leave nothing.  Hesitant or implausible calls surface instead of
        editing audio.
        """
        return [s.interval for s in self.segments if self.is_cut(s, policy, min_confidence, max_cut_duration)]

    @staticmethod
    def is_cut(
        segment: "AdSegment",
        policy: str = "promos",
        min_confidence: float = 0.5,
        max_cut_duration: float = DEFAULT_MAX_CUT_DURATION,
    ) -> bool:
        return (
            segment.category in POLICIES[policy]
            and segment.confidence >= min_confidence
            and segment.duration <= max_cut_duration
        )

    def to_dict(
        self,
        *,
        policy: str = "promos",
        min_confidence: float = 0.5,
        max_cut_duration: float = DEFAULT_MAX_CUT_DURATION,
    ) -> dict:
        cats = POLICIES[policy]
        segs = []
        for s in self.segments:
            d = s.to_dict()
            d["cut"] = self.is_cut(s, policy, min_confidence, max_cut_duration)
            segs.append(d)
        return {
            "schema": "podcleaner.ads/1",
            "model": self.model,
            "policy": policy,
            "min_confidence": min_confidence,
            "max_cut_duration": max_cut_duration,
            "cut_categories": sorted(cats),
            "duration": round(self.duration, 3),
            "cue_count": self.cue_count,
            "calls": self.calls,
            "chunks": self.chunks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "segments": segs,
        }


_EMBEDDED_JSON_RE = re.compile(r'\\?"(start_cue|end_cue|segments)\\?"\s*:')


def reply_looks_corrupted(raw: dict) -> Optional[str]:
    """Detect a syntactically valid reply whose *content* is broken.

    Seen 2026-09-06 from Claude Sonnet 5 via OpenRouter in ``json_schema`` mode: the
    first segment's ``reason`` string contained the remaining segments as escaped JSON,
    and a placeholder ``{"start_cue": 0, "end_cue": 0, "confidence": 0}`` completed the
    array.  ``json.loads`` accepted it and two advertisements were silently lost.
    Returns a short description of the problem, or ``None``.
    """
    items = raw.get("segments")
    if not isinstance(items, list):
        return None
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for key in ("reason", "category"):
            value = item.get(key)
            if isinstance(value, str) and _EMBEDDED_JSON_RE.search(value):
                return f"segment {i}: {key} contains embedded JSON (a truncated or mis-escaped reply)"
        if item.get("start_cue") == 0 and item.get("end_cue") == 0:
            return f"segment {i}: placeholder cue range 0-0"
        if str(item.get("reason", "")).strip().lower() == "placeholder":
            return f"segment {i}: placeholder segment"
    return None


def shape_warnings(analysis: AdAnalysis, policy: str) -> List[str]:
    """Sanity checks on the *shape* of an answer.  Seen from real models: every segment
    in one category the policy does not cut, producing a confident report that would
    cut nothing at all -- indistinguishable from an episode without ads."""
    out: List[str] = []
    cats = POLICIES[policy]
    if analysis.segments and not any(s.category in cats for s in analysis.segments):
        out.append(
            f"found {len(analysis.segments)} segments but none are cuttable under policy "
            f"{policy!r}; check the categories the model assigned"
        )
    used = {s.category for s in analysis.segments}
    if len(analysis.segments) >= 3 and len(used) == 1:
        out.append(
            f"every segment was classified {used.pop()!r}; a model that collapses "
            "categories may be guessing rather than distinguishing them"
        )
    return out


# --------------------------------------------------------------------------------------
# backend
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    model: str = "anthropic/claude-sonnet-5"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: Optional[str] = None
    #: Generous on purpose: the answer is a few hundred tokens, but a model that reasons
    #: bills that against the same budget and returns nothing when it runs out.
    max_tokens: int = 16000
    #: ``None`` omits the field (some backends reject it).
    temperature: Optional[float] = 0.0
    timeout: float = 600.0
    #: ``"none"`` disables thinking on reasoning models that would otherwise spend the
    #: whole token budget on it; ``None`` omits the field.
    reasoning_effort: Optional[str] = None
    #: OpenRouter-specific switch (``extra_body: {"reasoning": {"enabled": false}}``).
    #: Measured 2026-09-06: Claude Sonnet 5 via OpenRouter reasons adaptively by default
    #: and on two of five real transcripts exhausted an 8000-token budget without
    #: answering.  ``None`` leaves the backend default; only sent to OpenRouter.
    reasoning_enabled: Optional[bool] = False
    #: ``auto`` tries json_schema, then json_object, then plain text.
    response_format: str = "auto"
    #: Approximate input-token budget per call; longer transcripts are chunked.
    chunk_tokens: int = 60_000
    overlap_cues: int = 60

    @classmethod
    def from_env(cls, *, secrets_path: Optional[str] = None, **overrides) -> "LLMConfig":
        """Configuration from ``PODCLEANER_LLM_*`` / ``OPENROUTER_API_KEY`` / ``OPENAI_API_KEY``.

        When no key is in the environment, a JSON secrets file is consulted:
        ``secrets_path``, else ``$PODCLEANER_SECRETS_FILE``, else ``.secret.json`` in the
        repository root.  Accepted keys: ``openrouter-token``, ``OPENROUTER_API_KEY``,
        ``OPENAI_API_KEY``.  Pass ``secrets_path=""`` to disable the file lookup.
        """
        env = os.environ
        api_key = env.get("PODCLEANER_LLM_API_KEY") or env.get("OPENROUTER_API_KEY") or env.get("OPENAI_API_KEY")
        if not api_key:
            api_key = _key_from_secrets_file(secrets_path)
        values = dict(
            model=env.get("PODCLEANER_LLM_MODEL", env.get("AD_ANALYZER_MODEL", cls.model)),
            base_url=env.get("PODCLEANER_LLM_BASE_URL", env.get("OPENAI_BASE_URL", cls.base_url)),
            api_key=api_key,
        )
        values.update(overrides)
        return cls(**values)

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        if "openrouter" in self.base_url or "openai.com" in self.base_url or "anthropic" in self.base_url:
            raise LLMError(
                "no API key: set OPENROUTER_API_KEY / OPENAI_API_KEY / PODCLEANER_LLM_API_KEY",
                kind="auth",
            )
        return "not-needed"  # local servers ignore the key but the SDK requires one

    @property
    def label(self) -> str:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0]
        return f"{self.model}@{host}"

    def fingerprint(self) -> str:
        """Everything about a request that can change the reply, for caches and reports."""
        return json.dumps({
            "model": self.model, "base_url": self.base_url, "max_tokens": self.max_tokens,
            "temperature": self.temperature, "reasoning_effort": self.reasoning_effort,
            "reasoning_enabled": self.reasoning_enabled, "response_format": self.response_format,
        }, sort_keys=True)


_SECRET_KEYS = ("openrouter-token", "OPENROUTER_API_KEY", "openai-token", "OPENAI_API_KEY")


def _key_from_secrets_file(secrets_path: Optional[str]) -> Optional[str]:
    if secrets_path == "":
        return None
    candidates = [secrets_path] if secrets_path else [
        os.environ.get("PODCLEANER_SECRETS_FILE"),
        str(Path(__file__).resolve().parents[2] / ".secret.json"),
    ]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            for k in _SECRET_KEYS:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


def _response_format_for(mode: str) -> Optional[dict]:
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": "ad_segments", "strict": True, "schema": RESPONSE_SCHEMA},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


#: ``complete(system_prompt, user_prompt) -> (reply_text, usage_dict)``
CompleteFn = Callable[[str, str], Tuple[str, dict]]


class OpenAICompatibleClient:
    """Thin wrapper over the OpenAI SDK for any compatible endpoint."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self._client = None
        self._reasoning_switch_rejected = False

    def _sdk(self):
        if self._client is None:
            from openai import OpenAI  # lazy: offline callers never need the SDK

            headers = {"X-Title": "podcleaner"} if "openrouter" in self.config.base_url else None
            self._client = OpenAI(
                base_url=self.config.base_url,
                api_key=self.config.resolved_api_key(),
                timeout=self.config.timeout,
                default_headers=headers,
            )
        return self._client

    def complete(self, system_prompt: str, user_prompt: str) -> Tuple[str, dict]:
        import openai

        cfg = self.config
        ladder = ["json_schema", "json_object", "none"] if cfg.response_format == "auto" else [cfg.response_format]
        last_exc: Optional[Exception] = None
        for i, mode in enumerate(ladder):
            kwargs: dict = {
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": cfg.max_tokens,
            }
            if cfg.temperature is not None:
                kwargs["temperature"] = cfg.temperature
            rf = _response_format_for(mode)
            if rf is not None:
                kwargs["response_format"] = rf
            if cfg.reasoning_effort is not None:
                kwargs["reasoning_effort"] = cfg.reasoning_effort
            if cfg.reasoning_enabled is not None and "openrouter" in cfg.base_url and not self._reasoning_switch_rejected:
                kwargs["extra_body"] = {"reasoning": {"enabled": cfg.reasoning_enabled}}
            try:
                resp = self._sdk().chat.completions.create(**kwargs)
            except openai.BadRequestError as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "reasoning" in msg and "extra_body" in kwargs and not self._reasoning_switch_rejected:
                    # e.g. "Reasoning is mandatory for this endpoint and cannot be disabled":
                    # remember that for this client and resend the same request without it.
                    self._reasoning_switch_rejected = True
                    logger.warning("llm_reasoning_switch_rejected", model=cfg.model, error=str(exc)[:160])
                    kwargs.pop("extra_body", None)
                    try:
                        resp = self._sdk().chat.completions.create(**kwargs)
                    except openai.BadRequestError as exc2:
                        last_exc = exc2
                        msg = str(exc2).lower()
                        remaining = ladder[i + 1 :]
                        if remaining and rf is not None and not any(w in msg for w in ("context", "too long", "maximum")):
                            continue
                        raise _map_error(exc2, cfg) from exc2
                    except openai.OpenAIError as exc2:
                        raise _map_error(exc2, cfg) from exc2
                    else:
                        return self._extract(resp, cfg)
                remaining = ladder[i + 1 :]
                # A context-length rejection will fail identically on the next rung; any
                # other 400 with a structured-output request is worth one step down, since
                # backends word their "response_format unsupported" errors differently.
                if remaining and rf is not None and not any(w in msg for w in ("context", "too long", "maximum")):
                    logger.warning("llm_response_format_rejected", mode=mode, next=remaining[0], error=str(exc)[:200])
                    continue
                raise _map_error(exc, cfg) from exc
            except openai.OpenAIError as exc:
                raise _map_error(exc, cfg) from exc
            return self._extract(resp, cfg)
        raise LLMError(f"no response_format mode accepted: {last_exc}", kind="bad_request")

    @staticmethod
    def _extract(resp, cfg: LLMConfig) -> Tuple[str, dict]:
        if not getattr(resp, "choices", None):
            err = getattr(resp, "error", None)
            raise LLMError(f"model returned no choices ({err or 'no error given'}): {str(resp)[:300]}", kind="bad_reply")
        choice = resp.choices[0]
        content = choice.message.content
        usage = getattr(resp, "usage", None)
        details = getattr(usage, "completion_tokens_details", None) if usage else None
        usage_d = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "reasoning_tokens": (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
            "finish_reason": getattr(choice, "finish_reason", None),
        }
        if not content:
            reasoning = getattr(choice.message, "reasoning_content", None) or getattr(choice.message, "reasoning", None)
            hint = "raise max_tokens"
            if reasoning:
                hint = ("the model spent its budget reasoning and returned no answer; raise "
                        "max_tokens well above the reasoning length or set reasoning_effort='none'")
            raise LLMError(
                f"model returned empty content (finish_reason={choice.finish_reason!r}); {hint}",
                kind="empty",
            )
        return content, usage_d


def _map_error(exc: Exception, cfg: LLMConfig) -> LLMError:
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return LLMError(f"auth failed at {cfg.base_url}: {exc}", kind="auth")
    if isinstance(exc, openai.NotFoundError):
        return LLMError(f"model {cfg.model!r} not found at {cfg.base_url}: {exc}", kind="not_found")
    if isinstance(exc, openai.RateLimitError):
        return LLMError(f"rate limited or out of credit: {exc}", kind="rate_limit")
    if isinstance(exc, openai.BadRequestError):
        if "context" in str(exc).lower():
            return LLMError(f"prompt exceeds the model's context window: {exc}", kind="context")
        return LLMError(f"request rejected by {cfg.base_url}: {exc}", kind="bad_request")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(f"could not reach {cfg.base_url}: {exc}", kind="connection")
    if isinstance(exc, openai.APIStatusError):
        return LLMError(f"API error {exc.status_code}: {exc}", kind="status")
    return LLMError(str(exc), kind="error")


# --------------------------------------------------------------------------------------
# the classifier
# --------------------------------------------------------------------------------------


class AdClassifier:
    """Classify a transcript's cues into ad segments.

    ``complete`` may be injected (tests, other SDKs); by default an
    :class:`OpenAICompatibleClient` built from ``config`` is used.
    """

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        *,
        complete: Optional[CompleteFn] = None,
        max_retries: int = 1,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self._complete = complete or OpenAICompatibleClient(self.config).complete
        self.calls = 0
        #: how often a chunk is re-asked when its reply is detectably corrupted
        self.max_retries = max_retries
        #: appended to the system prompt (the cascade's screening pass uses it)
        self.system_prompt_suffix = ""

    def classify(self, transcript: Transcript) -> AdAnalysis:
        cues = list(transcript.cues)
        analysis = AdAnalysis(
            segments=[], model=self.config.label, cue_count=len(cues), duration=transcript.end
        )
        if not cues:
            analysis.warnings.append("empty transcript: nothing to classify")
            return analysis
        system_prompt = build_system_prompt() + self.system_prompt_suffix
        chunks = chunk_cues(cues, max_tokens=self.config.chunk_tokens, overlap_cues=self.config.overlap_cues)
        analysis.chunks = len(chunks)
        all_segments: List[AdSegment] = []
        for n, chunk in enumerate(chunks):
            user_prompt = render_transcript(chunk)
            if len(chunks) > 1:
                user_prompt = (
                    f"(Excerpt {n + 1} of {len(chunks)}: cues {chunk[0].index}-{chunk[-1].index}, "
                    f"{format_clock(chunk[0].start)}-{format_clock(chunk[-1].end)}.)\n" + user_prompt
                )
            logger.info("llm_request", chunk=n + 1, chunks=len(chunks), cues=len(chunk),
                        est_tokens=estimate_tokens(system_prompt + user_prompt), model=self.config.label)
            raw = None
            for attempt in range(1 + self.max_retries):
                prompt = user_prompt if attempt == 0 else (
                    user_prompt + "\n\n(Retry: the previous reply was malformed. Respond with the JSON object "
                    "only, with every segment as its own object in the segments array.)"
                )
                reply, usage = self._complete(system_prompt, prompt)
                self.calls += 1
                analysis.calls += 1
                analysis.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                analysis.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
                analysis.reasoning_tokens += int(usage.get("reasoning_tokens", 0) or 0)
                analysis.raw_replies.append(reply)
                try:
                    candidate = parse_model_json(reply)
                    problem = reply_looks_corrupted(candidate)
                except LLMError as exc:
                    if exc.kind != "bad_reply":
                        raise
                    candidate = {"segments": []}
                    problem = f"unparseable reply ({str(exc)[:120]}; finish_reason={usage.get('finish_reason')})"
                if problem is None:
                    raw = candidate
                    break
                logger.warning("llm_reply_corrupted", chunk=n + 1, attempt=attempt + 1, problem=problem)
                analysis.warnings.append(f"chunk {n + 1}: reply {attempt + 1} corrupted ({problem})"
                                         + ("; retried" if attempt < self.max_retries else "; giving up, partial result"))
                raw = candidate  # keep whatever is salvageable if every attempt is corrupted
                if attempt == self.max_retries:
                    analysis.degraded = True
            segments, warnings = resolve_segments(raw, chunk, chunk=n)
            analysis.warnings.extend(f"chunk {n + 1}: {w}" for w in warnings)
            all_segments.extend(segments)
            logger.info("llm_reply", chunk=n + 1, segments=len(segments), warnings=len(warnings),
                        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        merged, warnings = merge_segments(all_segments)
        analysis.segments = merged
        analysis.warnings.extend(warnings)
        analysis.warnings.extend(shape_warnings(analysis, "promos"))
        for s in merged:
            logger.info("ad_segment", start=round(s.start, 2), end=round(s.end, 2),
                        category=s.category, confidence=s.confidence, cues=f"{s.start_cue}-{s.end_cue}")
        return analysis
