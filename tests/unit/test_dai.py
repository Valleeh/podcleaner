"""podcleaner.eval.dai on synthetic MP3 frame streams: ground truth by construction.

We build a 'clean' stream of valid MPEG-1 Layer III frames with deterministic payloads,
splice 'ad' frames in at known positions, and require the oracle to recover exactly
those positions.  Negative controls: identical files give no regions; unrelated files
are refused rather than guessed at.
"""

from __future__ import annotations

import random

import pytest

from podcleaner.eval.dai import DaiError, find_inserted_regions, id3v2_size, iter_frames, parse_frame_header

# MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding -> 417-byte frames of 1152 samples
HEADER = bytes([0xFF, 0xFB, 0x90, 0x00])
FRAME_LEN = 417
FRAME_SECONDS = 1152 / 44100


def _frame(seed: int, tag: bytes = b"") -> bytes:
    rng = random.Random(seed)
    body = bytes(rng.randrange(256) for _ in range(FRAME_LEN - 4 - len(tag)))
    # avoid accidental sync words inside the payload
    body = body.replace(b"\xff", b"\x7f")
    return HEADER + tag + body


def _info_frame() -> bytes:
    return HEADER + b"\x00" * 32 + b"Info" + b"\x00" * (FRAME_LEN - 4 - 36)


def _stream(frames, id3: bytes = b"") -> bytes:
    return id3 + _info_frame() + b"".join(frames)


def _id3(size: int) -> bytes:
    return b"ID3\x04\x00\x00" + bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]) + b"\x00" * size


def test_frame_header_parsing_and_id3():
    f = parse_frame_header(HEADER + b"\x00" * 413, 0)
    assert f is not None and f.length == FRAME_LEN and f.samples == 1152 and f.sample_rate == 44100
    assert parse_frame_header(b"\x00\x00\x00\x00", 0) is None
    assert id3v2_size(_id3(100)) == 110
    assert id3v2_size(b"nope") == 0
    frames = list(iter_frames(_stream([_frame(i) for i in range(5)], _id3(20)), 30))
    assert len(frames) == 6  # info frame + 5


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_exact_inserted_regions(tmp_path):
    clean_frames = [_frame(i) for i in range(400)]
    ad1 = [_frame(10_000 + i) for i in range(50)]
    ad2 = [_frame(20_000 + i) for i in range(80)]
    stitched_frames = clean_frames[:100] + ad1 + clean_frames[100:300] + ad2 + clean_frames[300:]
    clean = _write(tmp_path, "clean.mp3", _stream(clean_frames, _id3(500)))
    stitched = _write(tmp_path, "stitched.mp3", _stream(stitched_frames, _id3(44)))

    res = find_inserted_regions(clean, stitched)
    assert len(res.regions) == 2
    r1, r2 = res.regions
    assert (r1.start_frame, r1.end_frame) == (100, 150)
    assert (r2.start_frame, r2.end_frame) == (350, 430)
    assert r1.start == pytest.approx(100 * FRAME_SECONDS) and r1.duration == pytest.approx(50 * FRAME_SECONDS)
    assert res.total_inserted == pytest.approx(res.duration_delta) == pytest.approx(130 * FRAME_SECONDS)
    assert res.matched_frames == 400 and res.modified_frames == 0
    # offset map: clean time before the first insert is unshifted, after it shifted by ad1
    assert res.to_stitched(10 * FRAME_SECONDS) == pytest.approx(10 * FRAME_SECONDS)
    assert res.to_stitched(150 * FRAME_SECONDS) == pytest.approx(200 * FRAME_SECONDS)
    assert res.to_clean(r1.start + 1e-6) is None
    assert res.to_clean(200 * FRAME_SECONDS) == pytest.approx(150 * FRAME_SECONDS)


def test_pre_roll_and_post_roll(tmp_path):
    clean_frames = [_frame(i) for i in range(200)]
    pre = [_frame(50_000 + i) for i in range(30)]
    post = [_frame(60_000 + i) for i in range(20)]
    clean = _write(tmp_path, "c.mp3", _stream(clean_frames))
    stitched = _write(tmp_path, "s.mp3", _stream(pre + clean_frames + post))
    res = find_inserted_regions(clean, stitched)
    assert [(r.start_frame, r.end_frame) for r in res.regions] == [(0, 30), (230, 250)]
    assert res.total_inserted == pytest.approx(res.duration_delta)


def test_rewritten_frame_at_splice_is_tolerated(tmp_path):
    clean_frames = [_frame(i) for i in range(200)]
    ad = [_frame(70_000 + i) for i in range(40)]
    # the stitcher re-encodes the clean frame right after the splice
    stitched_frames = clean_frames[:100] + ad + [_frame(999_999)] + clean_frames[101:]
    clean = _write(tmp_path, "c.mp3", _stream(clean_frames))
    stitched = _write(tmp_path, "s.mp3", _stream(stitched_frames))
    res = find_inserted_regions(clean, stitched)
    assert len(res.regions) == 1
    r = res.regions[0]
    assert r.start_frame == 100 and r.end_frame == 141 and r.skipped_clean_frames == 1
    assert r.skipped_clean_seconds == pytest.approx(FRAME_SECONDS)
    # the rewritten frame lies inside the region but is not inserted audio
    assert res.total_inserted == pytest.approx(res.duration_delta + FRAME_SECONDS)
    assert res.reconciled_inserted == pytest.approx(res.duration_delta)


def test_identical_files_have_no_regions(tmp_path):
    """Negative control."""
    data = _stream([_frame(i) for i in range(150)])
    a = _write(tmp_path, "a.mp3", data)
    b = _write(tmp_path, "b.mp3", data)
    res = find_inserted_regions(a, b)
    assert res.regions == [] and res.duration_delta == 0 and res.matched_frames == 150


def test_unrelated_files_are_refused(tmp_path):
    """Negative control: no shared content means no oracle, not a guess."""
    a = _write(tmp_path, "a.mp3", _stream([_frame(i) for i in range(100)]))
    b = _write(tmp_path, "b.mp3", _stream([_frame(5000 + i) for i in range(120)]))
    with pytest.raises(DaiError):
        find_inserted_regions(a, b)
