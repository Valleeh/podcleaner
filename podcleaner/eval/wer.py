"""Word error rate and time alignment between two transcripts of the same audio.

Used by the transcription integration tests to compare a whisper transcript against a
reference (the publisher's own transcript, where a feed carries one).  Two honest
caveats are built into the names here:

* The references we have are *silver*, not gold: they are machine transcripts of clean
  per-speaker studio tracks, not human proofreads.  A WER against them mixes our errors
  with theirs.  It is still a stable, repeatable number, which is what a regression gate
  needs.
* Normalisation is deliberately simple and symmetric (lower-case, punctuation and
  bracketed sound tags removed, dashes treated as spaces).  German compound spelling
  differences count as errors on both sides; numbers are left as written.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from podcleaner.transcripts import Cue, Transcript, Word

__all__ = [
    "Anchor",
    "PhraseMatch",
    "WerResult",
    "anchor_offsets",
    "find_phrase",
    "hypothesis_words",
    "inner_wer",
    "normalize_words",
    "window_wer",
    "word_error_rate",
]

_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|♪")
_DASH_RE = re.compile(r"[\-‐-―/]")
_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_STRAY_APOS_RE = re.compile(r"(?<!\w)'|'(?!\w)")


def normalize_words(text: str) -> List[str]:
    """Lower-case word list with punctuation, tags and sound annotations removed."""
    text = text.replace("’", "'").replace("`", "'").lower()
    text = _TAG_RE.sub(" ", text)
    text = _SOUND_RE.sub(" ", text)
    text = _DASH_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _STRAY_APOS_RE.sub(" ", text)
    text = text.replace("_", " ")
    return text.split()


@dataclass(frozen=True)
class WerResult:
    wer: float
    substitutions: int
    deletions: int
    insertions: int
    hits: int
    reference_words: int
    hypothesis_words: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def to_dict(self) -> dict:
        return {
            "wer": round(self.wer, 4),
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "hits": self.hits,
            "reference_words": self.reference_words,
            "hypothesis_words": self.hypothesis_words,
        }


def word_error_rate(reference: Union[str, Sequence[str]], hypothesis: Union[str, Sequence[str]]) -> WerResult:
    """WER of ``hypothesis`` against ``reference`` after :func:`normalize_words`.

    Raises ``ValueError`` on an empty reference: a rate over zero words is not a number
    and must not be scored silently.
    """
    import jiwer  # local import: only the evaluation path needs it

    ref = normalize_words(reference) if isinstance(reference, str) else list(reference)
    hyp = normalize_words(hypothesis) if isinstance(hypothesis, str) else list(hypothesis)
    if not ref:
        raise ValueError("reference has no words; WER is undefined")
    if not hyp:
        return WerResult(1.0, 0, len(ref), 0, 0, len(ref), 0)
    out = jiwer.process_words(" ".join(ref), " ".join(hyp))
    return WerResult(
        wer=float(out.wer),
        substitutions=int(out.substitutions),
        deletions=int(out.deletions),
        insertions=int(out.insertions),
        hits=int(out.hits),
        reference_words=len(ref),
        hypothesis_words=len(hyp),
    )


def inner_wer(reference: Union[str, Sequence[str]], hypothesis: Union[str, Sequence[str]], *, trim: int = 15,
              slack: int = 40) -> WerResult:
    """WER of the *inner* part of ``hypothesis`` against the best-matching span of ``reference``.

    For comparing two transcripts of the same audio where one was produced from a window:
    the words at the window edges are cut differently by the two runs (a cue straddling the
    edge is in one transcript and not the other), which a global alignment counts as
    errors.  The first and last ``trim`` hypothesis words are dropped and the reference
    span is chosen, within ``slack`` words of the same length, to minimise the rate, so
    what remains is recognition disagreement, not segmentation.
    """
    ref = normalize_words(reference) if isinstance(reference, str) else list(reference)
    hyp = normalize_words(hypothesis) if isinstance(hypothesis, str) else list(hypothesis)
    if len(hyp) <= 2 * trim + 5:
        raise ValueError(f"hypothesis too short ({len(hyp)} words) to trim {trim} words at each end")
    core = hyp[trim : len(hyp) - trim]
    n = len(core)
    best: Optional[WerResult] = None
    for lo in range(0, max(1, len(ref) - n + 1)):
        for extra in (-slack // 4, 0, slack // 4):
            hi = min(len(ref), max(lo + 1, lo + n + extra))
            span = ref[lo:hi]
            if not span:
                continue
            r = word_error_rate(span, core)
            if best is None or r.wer < best.wer:
                best = r
    assert best is not None
    return best


def window_wer(reference: Transcript, hypothesis: Transcript, start: float, end: float) -> WerResult:
    """WER over cues whose start lies in ``[start, end)`` in both transcripts."""
    ref = reference.window(start, end, mode="start_in").text()
    hyp = hypothesis.window(start, end, mode="start_in").text()
    return word_error_rate(ref, hyp)


# --------------------------------------------------------------------------------------
# time alignment
# --------------------------------------------------------------------------------------


def hypothesis_words(transcript: Transcript) -> List[Tuple[str, float]]:
    """``(normalized_word, start_time)`` for every word.  Uses word timings when the
    transcript has them; otherwise spreads a cue's words evenly over its duration."""
    out: List[Tuple[str, float]] = []
    for cue in transcript.cues:
        if cue.words:
            for w in cue.words:
                norm = normalize_words(w.text)
                if norm:
                    out.append((norm[0], w.start))
                    for extra in norm[1:]:
                        out.append((extra, w.start))
            continue
        words = normalize_words(cue.text)
        if not words:
            continue
        step = cue.duration / len(words) if cue.duration > 0 else 0.0
        for k, w in enumerate(words):
            out.append((w, cue.start + k * step))
    return out


@dataclass(frozen=True)
class Anchor:
    """The first ``n`` words of a reference cue located in the hypothesis."""

    text: str
    ref_time: float
    hyp_time: Optional[float]

    @property
    def delta(self) -> Optional[float]:
        return None if self.hyp_time is None else self.hyp_time - self.ref_time

    def to_dict(self) -> dict:
        return {"text": self.text, "ref_time": round(self.ref_time, 3),
                "hyp_time": None if self.hyp_time is None else round(self.hyp_time, 3),
                "delta": None if self.delta is None else round(self.delta, 3)}


def anchor_offsets(
    reference: Transcript,
    hypothesis: Transcript,
    *,
    n: int = 4,
    min_words: int = 6,
    search_radius: float = 20.0,
) -> List[Anchor]:
    """For each reference cue with at least ``min_words`` words, find its first ``n``
    words as an exact n-gram in the hypothesis within ``search_radius`` seconds and
    report where the hypothesis puts them.  Cues whose anchor is not found are reported
    with ``hyp_time=None`` -- a recognition error, not a timing one."""
    hyp = hypothesis_words(hypothesis)
    anchors: List[Anchor] = []
    for cue in reference.cues:
        words = normalize_words(cue.text)
        if len(words) < min_words:
            continue
        gram = words[:n]
        found: Optional[float] = None
        best_dist = None
        for k in range(len(hyp) - n + 1):
            if abs(hyp[k][1] - cue.start) > search_radius:
                continue
            if [w for w, _ in hyp[k : k + n]] == gram:
                dist = abs(hyp[k][1] - cue.start)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    found = hyp[k][1]
        anchors.append(Anchor(" ".join(gram), cue.start, found))
    return anchors


@dataclass(frozen=True)
class PhraseMatch:
    cue_index: int
    start: float
    end: float
    ratio: float
    text: str

    def to_dict(self) -> dict:
        return {"cue_index": self.cue_index, "start": round(self.start, 3),
                "end": round(self.end, 3), "ratio": round(self.ratio, 3), "text": self.text}


def find_phrase(
    transcript: Transcript,
    phrase: str,
    *,
    near: Optional[float] = None,
    radius: float = 90.0,
    min_ratio: float = 0.7,
    span: int = 2,
) -> Optional[PhraseMatch]:
    """Fuzzy-locate ``phrase`` in ``transcript``, looking across runs of up to ``span``
    consecutive cues so a phrase split over a cue boundary is still found.

    Returns the best match with similarity >= ``min_ratio``, located at the cue(s) that
    actually contain the matched words; ties prefer the match closest to ``near``.
    Similarity is difflib's ratio over normalised words, so a transcript that mangles one
    word in a ten-word line still matches.
    """
    target = normalize_words(phrase)
    if not target:
        return None
    cues = transcript.cues
    best: Optional[PhraseMatch] = None
    for i, cue in enumerate(cues):
        if near is not None and abs(cue.start - near) > radius:
            continue
        for k in range(1, span + 1):
            group = cues[i : i + k]
            words: List[str] = []
            owner: List[int] = []
            for gi, c in enumerate(group):
                for w in normalize_words(c.text):
                    words.append(w)
                    owner.append(gi)
            if not words:
                continue
            ratio, lo, hi = _best_subsequence_ratio(words, target)
            if ratio < min_ratio:
                continue
            first, last = group[owner[lo]], group[owner[max(lo, hi - 1)]]
            if near is not None and abs(first.start - near) > radius:
                continue  # the matched words themselves must be near, not just the group head
            candidate = PhraseMatch(first.index, first.start, last.end, ratio,
                                    " ".join(c.text for c in group[owner[lo] : owner[max(lo, hi - 1)] + 1]))
            if best is None or ratio > best.ratio + 1e-9 or (
                abs(ratio - best.ratio) < 1e-9 and near is not None
                and abs(candidate.start - near) < abs(best.start - near)
            ):
                best = candidate
    return best


def _best_subsequence_ratio(words: Sequence[str], target: Sequence[str]) -> Tuple[float, int, int]:
    """``(ratio, lo, hi)``: the highest difflib ratio between ``target`` and any window
    ``words[lo:hi]`` of about the target's length, so a long cue does not dilute a short
    phrase."""
    n = len(target)
    if len(words) <= n:
        return difflib.SequenceMatcher(None, list(words), list(target)).ratio(), 0, len(words)
    best, best_lo = 0.0, 0
    for lo in range(0, len(words) - n + 1):
        r = difflib.SequenceMatcher(None, list(words[lo : lo + n]), list(target)).ratio()
        if r > best:
            best, best_lo = r, lo
    return best, best_lo, best_lo + n
