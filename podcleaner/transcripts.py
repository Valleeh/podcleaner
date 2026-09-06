"""Transcript model shared by transcription, classification and evaluation.

A transcript is a list of :class:`Cue` objects -- timed spans of text, what an SRT or
VTT file calls a cue and what whisper.cpp calls a segment.  Cues optionally carry
:class:`Word` timings (whisper.cpp token timestamps grouped into words) and a speaker
label (from VTT ``<v Speaker>`` tags).

Parsers here are deliberately tolerant of the formats we actually meet:

* whisper.cpp ``-oj`` / ``-ojf`` JSON (``offsets`` in milliseconds, optional ``tokens``);
* SRT with ``,`` or ``.`` as the millisecond separator;
* WebVTT as published in podcast feeds (``<podcast:transcript>``), including per-speaker
  ``<v Track 1>`` voice tags and optional cue identifiers.

Nothing here interprets the text.  Deciding what is an advertisement is
:mod:`podcleaner.detect.llm`'s job; deciding where to cut is the boundary module's.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Union

__all__ = [
    "Cue",
    "Transcript",
    "TranscriptError",
    "Word",
    "format_clock",
    "format_timestamp",
    "is_non_speech",
    "load_transcript",
    "parse_srt",
    "parse_vtt",
    "parse_whisper_json",
]

PathLike = Union[str, Path]


class TranscriptError(ValueError):
    """Raised for malformed transcript input."""


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    #: whisper.cpp token probability of the first token of the word, when known.
    p: Optional[float] = None


@dataclass(frozen=True)
class Cue:
    """One timed span of text.  ``index`` is 1-based and local to its transcript."""

    index: int
    start: float
    end: float
    text: str
    speaker: Optional[str] = None
    words: Tuple[Word, ...] = ()

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass
class Transcript:
    cues: List[Cue]
    language: Optional[str] = None
    #: What was transcribed -- a path or URL.  Informational.
    source: Optional[str] = None
    #: Who produced it -- e.g. ``whisper.cpp/ggml-small-q5_1/greedy`` or ``official``.
    engine: Optional[str] = None
    #: Duration of the underlying audio when known; otherwise the end of the last cue.
    duration: Optional[float] = None
    meta: dict = field(default_factory=dict)

    # -- basic views --------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.cues)

    def __iter__(self) -> Iterator[Cue]:
        return iter(self.cues)

    @property
    def end(self) -> float:
        if self.duration is not None:
            return self.duration
        return self.cues[-1].end if self.cues else 0.0

    def text(self, separator: str = " ") -> str:
        return separator.join(c.text for c in self.cues)

    def words(self) -> List[Word]:
        out: List[Word] = []
        for cue in self.cues:
            out.extend(cue.words)
        return out

    # -- transformations ----------------------------------------------------------------

    def window(self, start: float, end: float, *, mode: str = "start_in") -> "Transcript":
        """Cues within ``[start, end)``.

        ``mode="start_in"`` keeps cues whose start lies in the window (the natural
        choice when comparing against another transcript of the same window);
        ``mode="overlap"`` keeps any cue that overlaps it.
        """
        if end < start:
            raise TranscriptError(f"window end {end} < start {start}")
        if mode == "start_in":
            kept = [c for c in self.cues if start <= c.start < end]
        elif mode == "overlap":
            kept = [c for c in self.cues if c.end > start and c.start < end]
        else:
            raise TranscriptError(f"unknown window mode {mode!r}")
        return Transcript(
            cues=_reindex(kept),
            language=self.language,
            source=self.source,
            engine=self.engine,
            duration=None,
            meta={**self.meta, "window": [start, end]},
        )

    def shifted(self, offset: float) -> "Transcript":
        """The same transcript with every timestamp moved by ``offset`` seconds."""
        cues = [
            replace(
                c,
                start=c.start + offset,
                end=c.end + offset,
                words=tuple(
                    Word(w.start + offset, w.end + offset, w.text, w.p) for w in c.words
                ),
            )
            for c in self.cues
        ]
        return Transcript(
            cues=cues,
            language=self.language,
            source=self.source,
            engine=self.engine,
            duration=None if self.duration is None else self.duration + offset,
            meta=dict(self.meta),
        )

    def without_words(self) -> "Transcript":
        return Transcript(
            cues=[replace(c, words=()) for c in self.cues],
            language=self.language,
            source=self.source,
            engine=self.engine,
            duration=self.duration,
            meta=dict(self.meta),
        )

    # -- serialisation ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema": "podcleaner.transcript/1",
            "language": self.language,
            "source": self.source,
            "engine": self.engine,
            "duration": self.duration,
            "meta": self.meta,
            "cues": [
                {
                    "index": c.index,
                    "start": round(c.start, 3),
                    "end": round(c.end, 3),
                    "text": c.text,
                    **({"speaker": c.speaker} if c.speaker else {}),
                    **(
                        {
                            "words": [
                                {
                                    "start": round(w.start, 3),
                                    "end": round(w.end, 3),
                                    "text": w.text,
                                    **({"p": round(w.p, 4)} if w.p is not None else {}),
                                }
                                for w in c.words
                            ]
                        }
                        if c.words
                        else {}
                    ),
                }
                for c in self.cues
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        if not isinstance(data, dict) or "cues" not in data:
            raise TranscriptError("transcript JSON must be an object with a 'cues' list")
        cues = []
        for i, raw in enumerate(data["cues"], start=1):
            words = tuple(
                Word(float(w["start"]), float(w["end"]), str(w["text"]), w.get("p"))
                for w in raw.get("words", [])
            )
            cues.append(
                Cue(
                    index=int(raw.get("index", i)),
                    start=float(raw["start"]),
                    end=float(raw["end"]),
                    text=str(raw["text"]),
                    speaker=raw.get("speaker"),
                    words=words,
                )
            )
        return cls(
            cues=cues,
            language=data.get("language"),
            source=data.get("source"),
            engine=data.get("engine"),
            duration=data.get("duration"),
            meta=dict(data.get("meta") or {}),
        )

    def save(self, path: PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: PathLike) -> "Transcript":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_srt(self) -> str:
        blocks = []
        for c in self.cues:
            blocks.append(
                f"{c.index}\n{format_timestamp(c.start, sep=',')} --> "
                f"{format_timestamp(c.end, sep=',')}\n{c.text}\n"
            )
        return "\n".join(blocks)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _reindex(cues: Sequence[Cue]) -> List[Cue]:
    return [replace(c, index=i) for i, c in enumerate(cues, start=1)]


def format_timestamp(seconds: float, *, sep: str = ".") -> str:
    """``HH:MM:SS.mmm`` (``sep=','`` for SRT)."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def format_clock(seconds: float) -> str:
    """Compact ``m:ss`` / ``h:mm:ss`` for prompts and reports."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_NON_SPEECH_RE = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪+.*♪+)\s*$")


def is_non_speech(text: str) -> bool:
    """True for cues that are only a sound tag such as ``[MUSIC]`` or ``(laughs)``."""
    return bool(_NON_SPEECH_RE.match(text)) or not text.strip()


_TIMING_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_TIMING_SHORT_RE = re.compile(r"(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2})[,.](\d{1,3})")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>")
_TAG_RE = re.compile(r"</?[^>]+>")


def _ts(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _parse_timing(line: str) -> Optional[Tuple[float, float]]:
    m = _TIMING_RE.search(line)
    if m:
        return _ts(*m.group(1, 2, 3, 4)), _ts(*m.group(5, 6, 7, 8))
    m = _TIMING_SHORT_RE.search(line)
    if m:
        return (
            _ts("0", m.group(1), m.group(2), m.group(3)),
            _ts("0", m.group(4), m.group(5), m.group(6)),
        )
    return None


def _blocks(text: str) -> Iterable[List[str]]:
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [ln.rstrip("\r") for ln in block.splitlines() if ln.strip()]
        if lines:
            yield lines


def parse_srt(text: str) -> List[Cue]:
    """Parse SRT.  Cue numbers in the file are ignored; indices are our own 1-based
    positions, so every index we hand out can be resolved locally."""
    cues: List[Cue] = []
    for lines in _blocks(text):
        timing_idx = next((i for i, ln in enumerate(lines) if _parse_timing(ln)), None)
        if timing_idx is None:
            continue
        start, end = _parse_timing(lines[timing_idx])  # type: ignore[misc]
        body = " ".join(ln.strip() for ln in lines[timing_idx + 1 :]).strip()
        cues.append(Cue(len(cues) + 1, start, end, body))
    return cues


def parse_vtt(text: str) -> List[Cue]:
    """Parse WebVTT.  ``<v Speaker>`` becomes :attr:`Cue.speaker`; other tags are
    stripped.  ``NOTE``/``STYLE``/``REGION`` blocks and the header are skipped."""
    if text.startswith("﻿"):
        text = text[1:]
    if not text.lstrip().startswith("WEBVTT"):
        raise TranscriptError("not a WebVTT file (missing WEBVTT header)")
    cues: List[Cue] = []
    for lines in _blocks(text):
        head = lines[0].strip()
        if head.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing_idx = next((i for i, ln in enumerate(lines) if _parse_timing(ln)), None)
        if timing_idx is None:
            continue
        start, end = _parse_timing(lines[timing_idx])  # type: ignore[misc]
        body = " ".join(ln.strip() for ln in lines[timing_idx + 1 :]).strip()
        speaker = None
        m = _VOICE_RE.search(body)
        if m:
            speaker = m.group(1).strip() or None
        body = _TAG_RE.sub("", body).strip()
        cues.append(Cue(len(cues) + 1, start, end, body, speaker=speaker))
    return cues


_SPECIAL_TOKEN_RE = re.compile(r"^\[_[A-Z]+_?\d*_?\]$|^<\|[^|]*\|>$")


def _group_words(tokens: Sequence[dict], offset: float) -> Tuple[Word, ...]:
    """Group whisper.cpp tokens into words: a token starting with a space (or the
    first text token) begins a new word; punctuation-only tokens attach to the previous
    word.  Special tokens like ``[_BEG_]`` are dropped."""
    words: List[Word] = []
    for tok in tokens:
        text = tok.get("text", "")
        if not text or _SPECIAL_TOKEN_RE.match(text.strip()):
            continue
        offs = tok.get("offsets") or {}
        t0 = offs.get("from", 0) / 1000.0 + offset
        t1 = offs.get("to", 0) / 1000.0 + offset
        p = tok.get("p")
        starts_word = text.startswith(" ") or not words
        if starts_word and text.strip():
            words.append(Word(t0, t1, text.strip(), p))
        elif words:
            prev = words[-1]
            words[-1] = Word(prev.start, max(prev.end, t1), prev.text + text.strip(), prev.p)
    return tuple(words)


def parse_whisper_json(data: dict, *, offset_seconds: float = 0.0) -> Transcript:
    """Parse whisper.cpp ``-oj``/``-ojf`` output.

    ``offset_seconds`` is added to every timestamp.  Note that whisper-cli's own ``-ot``
    offset is already included in its reported ``offsets``, so pass ``offset_seconds``
    only when the *audio file itself* was cut out of a longer recording (which is what
    :mod:`podcleaner.detect.transcribe` does, because ffmpeg ``-ss`` is much faster than
    decoding the whole file for whisper to skip).
    """
    if not isinstance(data, dict) or "transcription" not in data:
        raise TranscriptError("not whisper.cpp JSON output (missing 'transcription')")
    cues: List[Cue] = []
    for seg in data["transcription"]:
        offs = seg.get("offsets") or {}
        start = offs.get("from", 0) / 1000.0 + offset_seconds
        end = offs.get("to", 0) / 1000.0 + offset_seconds
        text = (seg.get("text") or "").strip()
        words = _group_words(seg.get("tokens") or [], offset_seconds)
        cues.append(Cue(len(cues) + 1, start, end, text, words=words))
    language = (data.get("result") or {}).get("language")
    params = data.get("params") or {}
    model = (data.get("model") or {}).get("type")
    return Transcript(
        cues=cues,
        language=language,
        engine=f"whisper.cpp/{Path(str(params.get('model', model or 'unknown'))).name}",
        meta={"whisper_params": params},
    )


def load_transcript(path: PathLike, *, kind: Optional[str] = None) -> Transcript:
    """Load ``.json`` (ours or whisper.cpp's), ``.srt`` or ``.vtt``."""
    p = Path(path)
    suffix = (kind or p.suffix.lstrip(".")).lower()
    text = p.read_text(encoding="utf-8")
    if suffix == "json":
        data = json.loads(text)
        if isinstance(data, dict) and "transcription" in data:
            t = parse_whisper_json(data)
        else:
            t = Transcript.from_dict(data)
        t.source = t.source or str(p)
        return t
    if suffix == "srt":
        cues = parse_srt(text)
        engine = "srt"
    elif suffix == "vtt":
        cues = parse_vtt(text)
        engine = "vtt"
    else:
        raise TranscriptError(f"unknown transcript format for {p}")
    return Transcript(cues=cues, source=str(p), engine=engine)
