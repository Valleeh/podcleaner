"""The episode this suite plays, built from nothing at the start of every run.

It is modelled on a real one -- Hacks On Tap, "Oh Canada! (With Jonathan Martin)", 73
minutes, as a podcatcher received it on 2026-08-31 with a pre-roll, two reads and a promo
block stitched in.  That episode is commercial and is never committed, so for as long as
the suite played it, five of these tests ran on this host and skipped everywhere else --
CI included, which is most of the point of having CI.  So the suite makes its own: 73
minutes of silence, and the transcript that says where the advertising in it is.

**Silence is not a shortcut, it is what this path actually works on.**  Nothing between
the download and the cut listens to the audio: the classifier reads the transcript, the
edges are its word timings, and the cutter is handed seconds.  The transcript is canned
data the suite writes in either case -- the real one was a recording of one API reply.  So
an episode of silence exercises every step the real one did, and the one thing it cannot
carry, speech, is the one thing nothing here was reading.  What is still real is the
cutting: these are ordinary MPEG frames at a podcast's bitrate, the server drops whole
ones the way it would a publisher's, and the served audio is measured by decoding it.

**A break never begins where a cue begins.**  This is the one property that makes the
suite worth running, and it is the one a generated episode loses by accident: cues here
are :data:`WORDS_PER_CUE` words long wherever that falls, so every break opens partway
through a cue that starts with the hosts still talking, and closes partway through
another.  A server that anchored a cut to the named cue's edges instead of to the words
the model quoted -- which is what this one did until 2026-09-11, and it cost 5.2 s of
programme on LdN -- then removes seconds of host speech, and the duration and chapter
assertions go red.  With breaks laid on cue boundaries the two are the same number and
nothing notices.  :func:`build` refuses to hand over an episode that has drifted back into
alignment.

The geometry is the real episode's, to the hundredth of a second, because the tests turn
on proportions rather than on content: the four breaks are 8.3% of the episode and have to
be cut, and a break claimed from the first advertisement to the last is 2503 s, which has
to be refused -- by the 600 s implausible-length rule first, and by the cap on the
fraction of an episode that may be removed if it ever got past that.

Imports nothing from the server: :data:`MARGIN_SECONDS` is read off ``docs/spec.md`` and
written again here, so a test can say what it expects without asking the server what it
intends to do.  The server is Elixir; this is one of the places that used to name it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from tests.integration.support import ffmpeg

#: How much of a break stays audible at each end: `docs/spec.md`, "a cut begins 1.5 s
#: after the break's first word and ends 1.5 s before its last".  Declared, not imported.
#:
#: What this suite measures is the seconds a cut removes, not which end the margin is
#: taken from: a server that took both margins at the tail would remove the same length,
#: shift the same chapter marks and drop the same cues, and pass everything here while
#: spending the whole head budget the margin exists to hold -- the room for the classifier
#: to quote a word early, or the transcriber to time one late.  Where the margin actually
#: sits is measured by `./run verify`, against the publisher's own master.
MARGIN_SECONDS = 1.5

#: A word of speech, and a cue's worth of them.  The real reply for this episode had 266
#: cues and 11,582 words over 73 minutes; this is the same cue length at a slower mouth.
WORD_SECONDS = 1.3
WORDS_PER_CUE = 13

#: What :func:`build` encodes at, and therefore how many bytes a second of this episode is.
#: A podcast's bitrate, and constant -- which is what lets the transcription stub say where
#: a piece of the audio begins by counting rather than by looking for it.
BITRATE_KBPS = 64

#: How far inside its cue a break has to begin, and how far before its last cue ends it
#: has to close -- each end on its own, because an edge is anchored on its own.  A cue
#: opens before the break inside it and closes after it, so this is what a cut anchored to
#: the cue would take that a cut anchored to the quoted words does not, and it has to be
#: far more than the second the duration assertion allows, let alone the 0.05 s the
#: chapter marks are held to.  A break can only be as deep as the episode in front of it:
#: the pre-roll opens this one 1.55 s in, as it does the real one, and is held to that.
STRADDLE_SECONDS = 2.0

#: A line of programme.  Its wording is asserted on: it is what proves the transcript the
#: listener is served is the programme's and not the advertising's.  Each break's own line
#: keeps its brand away from both ends, because a break keeps :data:`MARGIN_SECONDS`
#: audible at each end and those words survive into the served transcript.
PROGRAMME = "the panel keeps at it and the largest trading partner is Canada"


@dataclass(frozen=True)
class Run:
    """One stretch of the episode: advertising if it has a ``category``, else programme."""
    start: float
    end: float
    line: str
    category: str = ""


#: The episode, in order, at the real one's timings.  The two reads run straight into one
#: another there and do so here; the 0.38 s of air between them is the only detail dropped.
LAYOUT: Tuple[Run, ...] = (
    Run(0.00, 1.55, PROGRAMME),
    Run(1.55, 88.72, "a trailer for Northwind, another show from this network", "cross_promo"),
    Run(88.72, 974.01, PROGRAMME),
    Run(974.01, 1070.31, "a paid read for Cloudberry, who sell the news by the week", "sponsor_read"),
    Run(1070.31, 1196.01, "a paid read for Tidewater, a watch that counts your sleep", "sponsor_read"),
    Run(1196.01, 2435.77, PROGRAMME),
    Run(2435.77, 2504.57, "two trailers for Harbour Lights and Sandpiper, back to back", "cross_promo"),
    Run(2504.57, 4388.26, PROGRAMME),
)

#: Where the chapters are: the programme coming back after each break.
CHAPTER_TITLES = ("Oh Canada", "South Carolina", "J-Mart in Kansas")


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Cue:
    number: int          # 1-based, as the classifier is shown them
    words: Tuple[Word, ...]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Break:
    """One advertising block, and the words a classifier would quote to name its edges.

    ``start`` and ``end`` are the first and last quoted words' own timings, which is what
    a cut is anchored to.  ``first_cue`` and ``last_cue`` begin and end elsewhere: they
    are the cues those words happen to fall in, and they hold programme too.
    """
    category: str
    start: float
    end: float
    first_cue: int
    last_cue: int
    first_words: str
    last_words: str

    @property
    def cut_seconds(self) -> float:
        """What is removed: the quoted words' span less :data:`MARGIN_SECONDS` an end."""
        return (self.end - self.start) - 2 * MARGIN_SECONDS

    def as_segment(self, *, confidence: float = 0.95, reason: str = "") -> dict:
        """This break as the classifier reports it."""
        return {"start_cue": self.first_cue, "end_cue": self.last_cue,
                "category": self.category, "confidence": confidence,
                "reason": reason or f"{self.category} at {self.start:.0f}s",
                "first_words": self.first_words, "last_words": self.last_words}


@dataclass(frozen=True)
class Mark:
    """One chapter mark: the cue the classifier names, and where that cue begins."""
    cue: int
    title: str
    start: float


@dataclass(frozen=True)
class Episode:
    """The audio, the transcription reply for it, and what is where inside both."""
    mp3: Path
    transcript: bytes
    breaks: Tuple[Break, ...]
    chapters: Tuple[Mark, ...]
    seconds: float
    cues: Tuple["Cue", ...]

    def transcriber(self):
        """A transcription endpoint that answers about the audio it was actually sent.

        The server may send this episode whole or in pieces; an endpoint that refuses
        bodies over a limit forces the second.  Either way this answers the window of the
        episode those bytes are, timed from the window's own start, exactly as a real one
        would: it has no idea the audio is part of anything larger.

        So the offsetting is the *server's* arithmetic to get right.  Nothing here tells it
        where a piece belongs, and a piece put back in the wrong place makes the cut land
        in the wrong place, which the duration and chapter assertions see.

        Where a piece sits is counted, not searched for.  This episode is silence at a
        constant bitrate, so its frames are byte-identical and a piece cannot be located by
        looking for its bytes -- but a constant bitrate is exactly what makes counting
        exact: :data:`BITRATE_KBPS` is what :func:`build` told ffmpeg to encode, so a byte
        of audio is a known fraction of a second, and a piece begins where the pieces
        before it ended.

        That assumes the server sends the audio in order, once, and all of it.  A server
        that does not gets its pieces timed as if it had, which moves the cut and fails
        this test -- which is the right answer for any of those three going wrong.
        """
        per_second = BITRATE_KBPS * 1000 / 8
        state = {"sent": 0}

        def answer(posted: bytes) -> bytes:
            audio = _posted_audio(posted)
            start = state["sent"] / per_second
            state["sent"] += len(audio)
            stop = state["sent"] / per_second
            # A cue belongs to the piece its *first word* is in, so every cue is answered
            # exactly once and the numbering that comes back is the numbering the
            # classifier is shown.  A cue that runs past the end of its piece is reported
            # by it anyway: a real transcriber would end the cue at the audio and start
            # another, and modelling that here would renumber the cues out from under the
            # very segments this test asks the server to cut.
            window = [c for c in self.cues
                      if c.start >= start and (c.start < stop or stop >= self.seconds - 0.5)]
            return _reply(_rebased(window, start), len(audio) / per_second)

        return answer


def _words() -> List[List[Word]]:
    """The episode as timed words, one list per run, each filling its run exactly.

    A run is its line said over and over, and every saying is numbered at both ends:
    ``line7 ... end7``.  The server refuses a quote it can find twice in the cues it was
    given -- rightly -- so a fixture that repeated one line verbatim would test nothing
    but that refusal.
    """
    said = 0
    spoken: List[List[Word]] = []
    for run in LAYOUT:
        line = run.line.split()
        said += 1
        text: List[str] = [f"line{said}", *line, f"end{said}"]
        # Whole sayings, so a break's last three words always include its closing number
        # and stay unique -- but never more words than fit at a speaking pace, or a run
        # shorter than one saying squeezes it, and the straddle it gives the break after
        # it becomes an artefact of that squeeze rather than a fact about the layout.
        room = max(1, round((run.end - run.start) / WORD_SECONDS))
        while len(text) + len(line) + 2 <= room:
            said += 1
            text += [f"line{said}", *line, f"end{said}"]
        text = text[:room]
        step = (run.end - run.start) / len(text)
        spoken.append([Word(word, run.start + i * step, run.start + (i + 1) * step)
                       for i, word in enumerate(text)])
    return spoken


def _cues(spoken: Sequence[Sequence[Word]]) -> List[Cue]:
    """Every word of the episode, cut into cues of :data:`WORDS_PER_CUE`.

    Deliberately blind to where the runs are: a cue ends when it has enough words in it,
    so the cue a break starts in began while the hosts were still talking.  That is what
    the real transcriber does, and it is what tells an edge anchored to the quoted words
    apart from one anchored to the cue around them.
    """
    stream = [word for words in spoken for word in words]
    return [Cue(number=i + 1, words=tuple(stream[at:at + WORDS_PER_CUE]))
            for i, at in enumerate(range(0, len(stream), WORDS_PER_CUE))]


def _posted_audio(posted: bytes) -> bytes:
    """The file part of a multipart POST, without needing to know the rest of the form.

    The audio is whatever follows the blank line after the part headers that name a
    filename, up to the boundary that ends it.
    """
    boundary = posted.split(b"\r\n", 1)[0]
    for part in posted.split(boundary):
        head, _, body = part.partition(b"\r\n\r\n")
        if b"filename=" in head and body:
            return body.rsplit(b"\r\n", 1)[0]
    raise AssertionError("no file part in the posted body")


def _rebased(cues: Sequence[Cue], start: float) -> List[Cue]:
    """These cues with ``start`` taken off every time in them, and renumbered from 1.

    What a transcriber returns for a piece of audio: it is not told the piece came from
    anywhere, so it times it from its own beginning and numbers its cues from one.
    """
    return [Cue(number=i + 1,
                words=tuple(Word(w.text, w.start - start, w.end - start) for w in cue.words))
            for i, cue in enumerate(cues)]


def _reply(cues: Sequence[Cue], seconds: float) -> bytes:
    """The cues as the transcription API answers: timed segments and a flat word list."""
    return json.dumps({
        "task": "transcribe", "language": "english", "duration": round(seconds, 3),
        "text": " ".join(cue.text for cue in cues),
        "segments": [{"id": cue.number - 1, "start": round(cue.start, 3),
                      "end": round(cue.end, 3), "text": f" {cue.text}"} for cue in cues],
        "words": [{"word": f" {w.text}", "start": round(w.start, 3), "end": round(w.end, 3)}
                  for cue in cues for w in cue.words],
    }).encode("utf-8")


def build(directory: Path) -> Episode:
    """Write the episode into ``directory`` and say what is in it.

    The audio is silence at a podcast's bitrate, encoded by the same ffmpeg that will cut
    it.  It takes about fifteen seconds, so build it once for a run, not once per test.
    """
    spoken = _words()
    cues = _cues(spoken)
    end = LAYOUT[-1].end
    mp3 = directory / "episode.mp3"
    ffmpeg(["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
            "-t", f"{end:.3f}", "-c:a", "libmp3lame", "-b:a", f"{BITRATE_KBPS}k", "-y"],
           writes=mp3)

    def cue_of(word: Word) -> Cue:
        return next(c for c in cues if word in c.words)

    breaks = []
    for run, words in zip(LAYOUT, spoken):
        if not run.category:
            continue
        opens, closes = cue_of(words[0]), cue_of(words[-1])
        _refuse_alignment(run, words, opens, closes)

        breaks.append(Break(
            category=run.category, start=words[0].start, end=words[-1].end,
            first_cue=opens.number, last_cue=closes.number,
            first_words=" ".join(w.text for w in words[:3]),
            last_words=" ".join(w.text for w in words[-3:]),
        ))

    # A mark the programme comes back on: the first cue that begins after a break is over,
    # rather than the one the break ends inside -- that one starts mid-advertisement, is
    # dropped with the cut it sits in, and would leave the episode a chapter short.
    resumes = [after for before, after in zip(LAYOUT, LAYOUT[1:])
               if before.category and not after.category]
    chapters = tuple(
        Mark(cue=cue.number, title=title, start=cue.start)
        for cue, title in zip((next(c for c in cues if c.start >= run.start)
                               for run in resumes), CHAPTER_TITLES))

    return Episode(mp3=mp3, transcript=_reply(cues, end), breaks=tuple(breaks),
                   chapters=chapters, seconds=end, cues=tuple(cues))


def _refuse_alignment(run: Run, words: Sequence[Word], opens: Cue, closes: Cue) -> None:
    """Fail the build if this break begins or ends where its cue does.

    Not a case that has to be imagined: the first version of this file laid one cue per
    run, so every break sat exactly on cue boundaries and a cut anchored to the wrong one
    of the two removed the same seconds as a cut anchored to the right one.  The suite
    went green on a server that would have eaten the hosts' last sentence before every
    break.  This is the invariant that was silently lost, so it is checked rather than
    described.

    Each end is checked on its own.  Summing them would let a break with its start exactly
    on a cue boundary pass on the strength of its far end, and the start is the end the old
    bug ate from.  What a break cannot have is more programme in front of it than the
    episode holds, so the requirement at the head is the whole of what runs before the
    break, up to :data:`STRADDLE_SECONDS` -- 1.55 s for the pre-roll, as in the real
    episode, and the full margin for the other three.
    """
    head, tail = words[0].start - opens.start, closes.end - words[-1].end
    wanted = min(STRADDLE_SECONDS, run.start)
    if head < wanted - 1e-6 or tail < STRADDLE_SECONDS:
        raise AssertionError(
            f"the {run.category} break at {run.start:.2f}s opens {head:.2f}s into its cue "
            f"(wanted {wanted:.2f}s) and closes {tail:.2f}s before its last one ends "
            f"(wanted {STRADDLE_SECONDS}s): a cut anchored to the cue rather than to the "
            f"quoted words would pass every test in this suite")
