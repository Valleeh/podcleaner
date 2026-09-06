"""The hybrid transcript used by the ad tests: publisher cues mapped into the stitched
timeline plus whisper cues for the inserted regions."""

from __future__ import annotations

import pytest

from podcleaner.eval.dai import DaiResult, InsertedRegion
from podcleaner.transcripts import Cue, Transcript
from tests.integration.support import stitched_transcript


def _dai():
    # one 40 s insert at clean time 100 (stitched 100-140)
    region = InsertedRegion(100.0, 140.0, 0, 0, 0, 0)
    res = DaiResult([region], clean_duration=1000.0, stitched_duration=1040.0, clean_frames=0,
                    stitched_frames=0, matched_frames=0)
    res.offset_map = [(100.0, 40.0)]
    return res


def test_cues_are_shifted_split_at_the_splice_and_interleaved_with_the_insert():
    official = Transcript([Cue(1, 90.0, 95.0, "before"), Cue(2, 96.0, 104.0, "straddles the splice"),
                           Cue(3, 105.0, 110.0, "after")], language="de")
    insert = Transcript([Cue(1, 100.0, 120.0, "Werbung."), Cue(2, 120.0, 139.0, "Kaufen Sie.")])
    h = stitched_transcript(official, _dai(), [insert])
    spans = [(c.index, round(c.start, 1), round(c.end, 1), c.text) for c in h.cues]
    assert spans == [
        (1, 90.0, 95.0, "before"),
        (2, 96.0, 100.0, "straddles the splice"),
        (3, 100.0, 120.0, "Werbung."),
        (4, 120.0, 139.0, "Kaufen Sie."),
        (5, 140.0, 144.0, "…"),
        (6, 145.0, 150.0, "after"),
    ]
    assert h.duration == 1040.0 and h.language == "de"
    assert max(c.duration for c in h.cues) < 40, "no cue is stretched across the insert"


def test_continuation_cues_become_dont_care_spans():
    from tests.integration.support import dont_care_from_transcript

    official = Transcript([Cue(1, 96.0, 104.0, "straddles the splice")])
    h = stitched_transcript(official, _dai(), [Transcript([Cue(1, 100.0, 139.0, "ad")])])
    assert h.meta["continuation_cues"] == [(140.0, 144.0)]
    [dc] = dont_care_from_transcript(h)
    assert dc.ambiguous and (dc.start, dc.end) == (140.0, 144.0)
