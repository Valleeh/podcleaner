"""Audio fingerprint tier: a landmark-hash index of known ad creatives.

Why this tier exists
--------------------

Podcast ad creatives repeat.  The *same* 30-second spot is dropped into dozens of
episodes, byte-for-byte identical apart from the encoder.  Once a creative has been
identified once -- expensively, by a transcript + LLM pass -- every subsequent encounter
should be a cheap lookup.  That is the compounding claim in the design memo and it is
what :class:`FingerprintTier` implements: fingerprint first, escalate only on a miss.

Design
------

*Landmark hashing* (the Shazam scheme), implemented in numpy.  ``fpcalc``/chromaprint is
**not** installed on this machine and we deliberately do not shell out to it.

1. Decode to 16 kHz mono float32 with ffmpeg (the one external tool we do rely on, and
   the only thing that can read mp3).
2. Short-time Fourier transform, Hann window, ``N_FFT`` 1024 / hop 256 (16 ms frames).
3. Pick spectral peaks: local maxima under a separable 2-D max filter, above an
   adaptive per-frame and global floor, capped per frame.
4. Pair each anchor peak with a few later peaks in a target zone; pack
   ``(f1, f2, dt)`` into one integer hash.  Store ``(hash, anchor_frame)``.

Matching is two stage, and the second stage is the one that keeps the false-positive
rate at zero:

* **Candidate generation.** Join query hashes against the SQLite posting list and
  histogram ``query_frame - reference_frame``.  A real occurrence puts every one of its
  votes into a *single* offset bin; chance collisions smear across all bins.
* **Verification.** For each candidate offset, compare the reference and query mel
  spectra directly:

  - ``shape``  -- mean per-frame cosine of L2-normalised mel energies.  Invariant to
    gain by construction (per-frame normalisation), so ±3 dB is free.
  - ``struct`` -- Pearson correlation of the *mean-removed* log-mel matrices.  Removing
    each band's mean over the window strips the stationary spectral envelope and leaves
    only the temporal fine structure, which is specific to *this waveform*.  This is
    what separates "the same violet-noise ad" from "some other violet noise": both have
    an identical average spectrum, so ``shape`` alone would accept the impostor.

  ``struct`` is only meaningful when the reference actually has temporal structure.  A
  mathematically constant tone has none -- two constant tones at the same frequency are
  genuinely indistinguishable -- so when the reference's own structural energy is below
  ``struct_floor`` the structural test is skipped and recorded as ``None``.

The asymmetry from the memo (a false cut is ~3x worse than a missed ad) is baked into
the defaults: thresholds are set where a *true* match still clears them comfortably but
nothing else comes close.  See ``tests/rebuild/test_fingerprint.py`` for the measured
margins.

Storage
-------

SQLite.  :func:`podcleaner.core.db.connect` is reused when importable (it sets WAL,
``busy_timeout`` and ``foreign_keys``, which is exactly what we want); if it is not
importable this module falls back to an equivalent local ``connect`` so the fingerprint
tier has no hard dependency on step 2.  The tables (``fp_creatives``, ``fp_hashes``,
``fp_meta``) are self-contained and are created by this module, not by
``core.db.migrate``; they can therefore live in their own file or share the pipeline
database without colliding with it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

__all__ = [
    "ALGO_VERSION",
    "DEFAULT_PARAMS",
    "DEFAULT_THRESHOLDS",
    "FingerprintError",
    "FingerprintLibrary",
    "FingerprintParams",
    "FingerprintTier",
    "Fingerprint",
    "Match",
    "MatchThresholds",
    "TierResult",
    "decode_audio",
    "fingerprint_file",
    "fingerprint_samples",
]

#: Bumped whenever the extraction algorithm changes.  Stored with every creative so a
#: library built by an older version is rejected rather than silently mis-matched.
ALGO_VERSION = 1


class FingerprintError(RuntimeError):
    """Decoding, extraction or library errors."""


# --------------------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FingerprintParams:
    """Extraction knobs.  Frozen: two fingerprints are only comparable if these match."""

    sample_rate: int = 16_000
    n_fft: int = 1024
    hop: int = 256
    #: Local-maximum neighbourhood, in (frequency bins, frames).
    peak_radius_freq: int = 12
    peak_radius_time: int = 6
    #: A peak must be within this many dB of its own frame's maximum ...
    peak_frame_db: float = 35.0
    #: ... and within this many dB of the whole file's maximum.
    peak_global_db: float = 70.0
    max_peaks_per_frame: int = 4
    #: Target zone for pairing: dt in [1, fan_time], |df| <= fan_freq bins.
    fan_time: int = 32
    fan_freq: int = 96
    fan_out: int = 6
    #: Mel filterbank used by the verification stage.
    n_mels: int = 40
    mel_fmin: float = 50.0
    mel_fmax: float = 7800.0

    @property
    def frame_seconds(self) -> float:
        return self.hop / self.sample_rate

    def frames_to_seconds(self, frames: float) -> float:
        """Frame index -> seconds.  Frame ``t`` begins at sample ``t * hop``.

        The STFT is *not* centred (no padding), so this is the whole of the offset
        arithmetic and there is no half-window correction to forget.
        """
        return frames * self.hop / self.sample_rate

    def seconds_to_frames(self, seconds: float) -> float:
        return seconds * self.sample_rate / self.hop

    def key(self) -> bytes:
        """Deterministic byte encoding of the parameter set, for the fingerprint blob."""
        return struct.pack(
            "<iiiiiddiiiiidd",
            self.sample_rate,
            self.n_fft,
            self.hop,
            self.peak_radius_freq,
            self.peak_radius_time,
            self.peak_frame_db,
            self.peak_global_db,
            self.max_peaks_per_frame,
            self.fan_time,
            self.fan_freq,
            self.fan_out,
            self.n_mels,
            self.mel_fmin,
            self.mel_fmax,
        )


DEFAULT_PARAMS = FingerprintParams()


@dataclass(frozen=True)
class MatchThresholds:
    """Acceptance thresholds.

    Tuned against the step 1 synthetic corpus.  ``S4.3`` (zero false positives on
    ad-free content) is the binding constraint; the measured separation is recorded in
    ``tests/rebuild/test_fingerprint.py::test_s43_threshold_margin_is_reported``.
    """

    #: Minimum votes in the winning offset bin.
    min_votes: int = 40
    #: Winning bin must be this many times the mean of the non-empty bins for that
    #: creative.  Chance collisions are flat; a real hit is a spike.
    min_vote_ratio: float = 4.0
    #: Minimum fraction of the reference's own hashes that must land in the bin.
    #: Deliberately weak: a splice that is off the analysis grid halves the number of
    #: reproducible peaks, so this is a sanity floor, not the discriminator.
    min_vote_coverage: float = 0.04
    #: Mean per-frame mel cosine.  Gain invariant.
    min_shape: float = 0.98
    #: Fraction of frames whose mel cosine is at least ``FRAME_AGREE_COSINE``.  The
    #: sharpest of the three verification statistics.
    min_frame_agreement: float = 0.90
    #: Pearson r of the mean-removed log-mel matrices, when applicable.
    min_struct: float = 0.55
    #: Below this per-band structural std (in log units) the reference is treated as
    #: stationary and the structural test is skipped.
    struct_floor: float = 0.08
    #: Overall confidence gate.
    min_confidence: float = 0.60
    #: How many offset bins per creative are worth verifying.  Verification is cheap;
    #: a repetitive creative can tie across many bins, so this is generous.
    max_candidates: int = 48
    #: Reject matches shorter than this after clipping to the query.
    min_match_seconds: float = 2.0
    #: Fraction of the reference that must be present in the query.
    min_overlap_fraction: float = 0.60


DEFAULT_THRESHOLDS = MatchThresholds()


# --------------------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------------------


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:  # pragma: no cover - ffmpeg is a documented prerequisite
        raise FingerprintError("ffmpeg not found on PATH")
    return path


def decode_audio(path: "str | os.PathLike[str]", sample_rate: int = 16_000) -> np.ndarray:
    """Decode any ffmpeg-readable file to mono float32 at ``sample_rate``.

    Returns a 1-D array in [-1, 1].  Deterministic: ffmpeg's PCM/mp3 decoders are.
    """
    src = Path(path)
    if not src.exists():
        raise FingerprintError(f"no such audio file: {src}")
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(src),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise FingerprintError(
            f"ffmpeg could not decode {src}: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32, copy=True)


# --------------------------------------------------------------------------------------
# signal processing
# --------------------------------------------------------------------------------------


def _hann(n: int) -> np.ndarray:
    """Periodic Hann window (matches the STFT convention; ``np.hanning`` is symmetric)."""
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n, dtype=np.float64) / n)).astype(
        np.float64
    )


def _stft_power(x: np.ndarray, params: FingerprintParams) -> np.ndarray:
    """Power spectrogram, shape ``(bins, frames)``.  Frame ``t`` starts at sample ``t*hop``."""
    n_fft, hop = params.n_fft, params.hop
    if x.size < n_fft:
        x = np.pad(x, (0, n_fft - x.size))
    n_frames = 1 + (x.size - n_fft) // hop
    frames = sliding_window_view(x.astype(np.float64, copy=False), n_fft)[:: hop][:n_frames]
    spec = np.fft.rfft(frames * _hann(n_fft), axis=-1)
    return (spec.real**2 + spec.imag**2).T


def _max_filter1d(a: np.ndarray, axis: int, radius: int) -> np.ndarray:
    if radius <= 0:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (radius, radius)
    padded = np.pad(a, pad, mode="constant", constant_values=-np.inf)
    return sliding_window_view(padded, 2 * radius + 1, axis=axis).max(axis=-1)


def _mel_filterbank(params: FingerprintParams) -> np.ndarray:
    """Triangular mel filterbank, shape ``(n_mels, bins)``.  Hand-rolled: no librosa."""

    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (np.asarray(m, dtype=np.float64) / 2595.0) - 1.0)

    n_bins = params.n_fft // 2 + 1
    edges_hz = mel_to_hz(
        np.linspace(hz_to_mel(params.mel_fmin), hz_to_mel(params.mel_fmax), params.n_mels + 2)
    )
    freqs = np.fft.rfftfreq(params.n_fft, d=1.0 / params.sample_rate)
    fb = np.zeros((params.n_mels, n_bins), dtype=np.float64)
    for m in range(params.n_mels):
        lo, mid, hi = edges_hz[m], edges_hz[m + 1], edges_hz[m + 2]
        left = (freqs - lo) / max(mid - lo, 1e-9)
        right = (hi - freqs) / max(hi - mid, 1e-9)
        fb[m] = np.clip(np.minimum(left, right), 0.0, None)
    return fb


_MEL_CACHE: dict[bytes, np.ndarray] = {}


def _mel(power: np.ndarray, params: FingerprintParams) -> np.ndarray:
    """Mel energies, shape ``(frames, n_mels)``, float32."""
    key = params.key()
    fb = _MEL_CACHE.get(key)
    if fb is None:
        fb = _mel_filterbank(params)
        _MEL_CACHE[key] = fb
    return (fb @ power).T.astype(np.float32)


def _pick_peaks(power: np.ndarray, params: FingerprintParams) -> tuple[np.ndarray, np.ndarray]:
    """Spectral peaks as ``(frames, bins)`` index arrays, sorted by (frame, bin)."""
    db = 10.0 * np.log10(np.maximum(power, 1e-20))
    local_max = _max_filter1d(
        _max_filter1d(db, axis=0, radius=params.peak_radius_freq),
        axis=1,
        radius=params.peak_radius_time,
    )
    is_peak = db >= local_max  # >= so a perfectly flat ridge still yields its own bin
    frame_floor = db.max(axis=0, keepdims=True) - params.peak_frame_db
    global_floor = db.max() - params.peak_global_db
    is_peak &= db >= frame_floor
    is_peak &= db >= global_floor
    bins, frames = np.nonzero(is_peak)

    if bins.size == 0:
        return frames, bins

    # cap per frame: keep the loudest `max_peaks_per_frame`.  Deterministic tie-break on
    # bin index so the same input always yields the same peak set.
    order = np.lexsort((bins, -db[bins, frames], frames))
    frames, bins = frames[order], bins[order]
    # rank within frame
    starts = np.searchsorted(frames, frames, side="left")
    rank = np.arange(frames.size) - starts
    keep = rank < params.max_peaks_per_frame
    frames, bins = frames[keep], bins[keep]
    order = np.lexsort((bins, frames))
    return frames[order], bins[order]


def _landmarks(
    frames: np.ndarray, bins: np.ndarray, params: FingerprintParams
) -> np.ndarray:
    """Pack peak pairs into ``(n, 2)`` int64 ``[hash, anchor_frame]``, sorted.

    Hash layout (28 bits): ``f1q(9) | f2q(9) | dt(6)`` with ``fq = bin >> 1``.
    """
    if frames.size == 0:
        return np.zeros((0, 2), dtype=np.int64)

    out_hash: list[np.ndarray] = []
    out_time: list[np.ndarray] = []
    n = frames.size
    # For each anchor i, targets are the next peaks in time order.  Because peaks are
    # sorted by frame, targets live in a contiguous slice starting at i+1.
    hi = np.searchsorted(frames, frames + params.fan_time, side="right")
    for shift in range(1, params.fan_out * params.max_peaks_per_frame + 1):
        j = np.arange(n) + shift
        valid = (j < n) & (j < hi)
        if not valid.any():
            break
        i_idx = np.nonzero(valid)[0]
        j_idx = j[valid]
        dt = frames[j_idx] - frames[i_idx]
        df = bins[j_idx] - bins[i_idx]
        ok = (dt >= 1) & (dt <= params.fan_time) & (np.abs(df) <= params.fan_freq)
        if not ok.any():
            continue
        i_idx, j_idx, dt = i_idx[ok], j_idx[ok], dt[ok]
        f1 = (bins[i_idx] >> 1).astype(np.int64) & 0x1FF
        f2 = (bins[j_idx] >> 1).astype(np.int64) & 0x1FF
        h = (f1 << 15) | (f2 << 6) | (dt.astype(np.int64) & 0x3F)
        out_hash.append(h)
        out_time.append(frames[i_idx].astype(np.int64))

    if not out_hash:
        return np.zeros((0, 2), dtype=np.int64)
    pairs = np.stack([np.concatenate(out_hash), np.concatenate(out_time)], axis=1)
    pairs = np.unique(pairs, axis=0)  # sorted + deduplicated => deterministic
    return pairs


# --------------------------------------------------------------------------------------
# fingerprint object
# --------------------------------------------------------------------------------------

_MAGIC = b"PCFP"


@dataclass
class Fingerprint:
    """Landmark hashes plus the mel profile used for verification."""

    pairs: np.ndarray  # (n, 2) int64: [hash, anchor_frame]
    mel: np.ndarray  # (frames, n_mels) float32, frame origin = sample 0
    n_samples: int
    params: FingerprintParams = DEFAULT_PARAMS
    algo_version: int = ALGO_VERSION
    #: Extra views of the same audio whose frame grid starts at a non-zero sample
    #: offset, as ``(sample_offset, mel)`` / ``(sample_offset, pairs)``.  Only the
    #: *query* side needs these -- see :data:`VERIFY_PHASES` -- and they are
    #: deliberately **not** serialised, so ``to_bytes`` stays a pure function of the
    #: audio and the parameters.
    extra_mels: tuple = ()
    extra_pairs: tuple = ()
    n_phases: int = 1

    def mel_views(self) -> list:
        """``[(sample_offset, mel), ...]``, phase 0 first."""
        return [(0, self.mel), *self.extra_mels]

    @property
    def sub_hop(self) -> int:
        """Size of one sub-frame unit, in samples (``hop / n_phases``)."""
        return self.params.hop // self.n_phases

    def query_positions(self) -> np.ndarray:
        """``(n, 2)`` int64 ``[hash, position]`` over every phase.

        ``position`` counts :attr:`sub_hop` units from sample 0, so hashes extracted on
        different frame grids share one coordinate system and can be histogrammed
        together.  Reference frames are scaled by ``n_phases`` at join time.
        """
        k = self.n_phases
        blocks = [np.stack([self.pairs[:, 0], self.pairs[:, 1] * k], axis=1)]
        for shift, pr in self.extra_pairs:
            if pr.shape[0] == 0:
                continue
            phase = shift // max(self.sub_hop, 1)
            blocks.append(np.stack([pr[:, 0], pr[:, 1] * k + phase], axis=1))
        return np.concatenate(blocks, axis=0) if blocks else np.zeros((0, 2), np.int64)

    @property
    def n_frames(self) -> int:
        return int(self.mel.shape[0])

    @property
    def duration_seconds(self) -> float:
        return self.n_samples / self.params.sample_rate

    def to_bytes(self) -> bytes:
        """Deterministic serialisation.

        Identical input audio -> identical bytes (S4.5).  Everything here is either an
        integer, a fixed parameter encoding, or a little-endian float32 buffer produced
        by the same deterministic numpy code path.
        """
        header = _MAGIC + struct.pack(
            "<IIIII",
            self.algo_version,
            self.n_samples,
            int(self.pairs.shape[0]),
            int(self.mel.shape[0]),
            int(self.mel.shape[1]),
        )
        return (
            header
            + self.params.key()
            + np.ascontiguousarray(self.pairs, dtype="<i8").tobytes()
            + np.ascontiguousarray(self.mel, dtype="<f4").tobytes()
        )

    @classmethod
    def from_bytes(cls, blob: bytes, params: FingerprintParams = DEFAULT_PARAMS) -> "Fingerprint":
        if blob[:4] != _MAGIC:
            raise FingerprintError("not a fingerprint blob")
        algo, n_samples, n_pairs, n_frames, n_mels = struct.unpack("<IIIII", blob[4:24])
        klen = len(params.key())
        off = 24 + klen
        stored_key = blob[24:off]
        if stored_key != params.key():
            raise FingerprintError("fingerprint was built with different parameters")
        pair_bytes = n_pairs * 16
        pairs = np.frombuffer(blob[off : off + pair_bytes], dtype="<i8").reshape(n_pairs, 2)
        mel = np.frombuffer(blob[off + pair_bytes :], dtype="<f4").reshape(n_frames, n_mels)
        return cls(
            pairs=np.array(pairs),
            mel=np.array(mel),
            n_samples=int(n_samples),
            params=params,
            algo_version=int(algo),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()


#: Sub-hop phases the verifier evaluates on the query side.
#:
#: A splice does not respect our 256-sample frame grid.  When an ad starts half a hop
#: off the grid, every analysis frame straddles the same audio differently from the
#: reference and the measured similarity collapses -- for a transient-rich creative it
#: fell from 1.00 to 0.88, i.e. below any threshold that also excludes impostors.  So
#: the query is analysed at four frame origins (0, hop/4, hop/2, 3*hop/4) and the
#: verifier takes the best.  This also refines the reported offset to hop/4 = 4 ms.
VERIFY_PHASES = 4


def fingerprint_samples(
    samples: np.ndarray,
    params: FingerprintParams = DEFAULT_PARAMS,
    *,
    n_phases: int = 1,
) -> Fingerprint:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    power = _stft_power(x, params)
    frames, bins = _pick_peaks(power, params)
    extra_mels, extra_pairs = [], []
    for p in range(1, max(n_phases, 1)):
        shift = (p * params.hop) // n_phases
        if shift <= 0 or shift >= x.size:
            continue
        pw = _stft_power(x[shift:], params)
        pf, pb = _pick_peaks(pw, params)
        extra_mels.append((shift, _mel(pw, params)))
        extra_pairs.append((shift, _landmarks(pf, pb, params)))
    return Fingerprint(
        pairs=_landmarks(frames, bins, params),
        mel=_mel(power, params),
        n_samples=int(x.size),
        params=params,
        extra_mels=tuple(extra_mels),
        extra_pairs=tuple(extra_pairs),
        n_phases=max(n_phases, 1),
    )


def fingerprint_file(
    path: "str | os.PathLike[str]",
    params: FingerprintParams = DEFAULT_PARAMS,
    *,
    n_phases: int = 1,
) -> Fingerprint:
    return fingerprint_samples(
        decode_audio(path, params.sample_rate), params, n_phases=n_phases
    )


def _as_fingerprint(
    audio: "str | os.PathLike[str] | np.ndarray | Fingerprint",
    params: FingerprintParams,
    *,
    n_phases: int = 1,
) -> Fingerprint:
    if isinstance(audio, Fingerprint):
        if audio.params != params:
            raise FingerprintError("fingerprint parameter mismatch")
        return audio
    if isinstance(audio, np.ndarray):
        return fingerprint_samples(audio, params, n_phases=n_phases)
    return fingerprint_file(audio, params, n_phases=n_phases)


# --------------------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------------------


#: A frame counts as "agreeing" when its mel cosine is at least this.
FRAME_AGREE_COSINE = 0.995


def _shape_struct(
    ref_mel: np.ndarray, qry_mel: np.ndarray
) -> tuple[float, float, Optional[float], float]:
    """Return ``(shape, agree, struct, ref_structural_std)`` for two aligned mel windows.

    ``shape``  : mean per-frame cosine of L2-normalised linear mel energies.  Scaling
                 either signal by a constant leaves every frame's unit vector unchanged,
                 so this is exactly gain invariant.
    ``struct`` : Pearson r of the log-mel matrices after removing each band's mean over
                 the window.  A constant gain is an additive constant in log space and
                 is removed by the same subtraction, so this is gain invariant too.
                 ``None`` when the reference carries no temporal structure to test.
    """
    n = min(ref_mel.shape[0], qry_mel.shape[0])
    if n == 0:
        return 0.0, 0.0, None, 0.0
    a = ref_mel[:n].astype(np.float64)
    b = qry_mel[:n].astype(np.float64)

    na = np.linalg.norm(a, axis=1)
    nb = np.linalg.norm(b, axis=1)
    live = (na > 1e-12) & (nb > 1e-12)
    if not live.any():
        return 0.0, 0.0, None, 0.0
    cos = np.clip(np.sum(a[live] * b[live], axis=1) / (na[live] * nb[live]), -1.0, 1.0)
    shape = float(np.mean(cos))
    # The *fraction* of frames that agree is far more discriminative than their mean.
    # An impostor that shares a carrier but differs in a short recurring event (a beep
    # at another pitch) still averages ~0.96; it fails badly on the frames where the
    # event happens, and this statistic sees exactly those frames.
    agree = float(np.count_nonzero(cos >= FRAME_AGREE_COSINE)) / float(n)

    la = np.log(a + 1e-10)
    lb = np.log(b + 1e-10)
    ca = la - la.mean(axis=0, keepdims=True)
    cb = lb - lb.mean(axis=0, keepdims=True)
    ref_std = float(np.sqrt(np.mean(ca**2)))
    denom = float(np.linalg.norm(ca) * np.linalg.norm(cb))
    struct = float((ca * cb).sum() / denom) if denom > 1e-12 else None
    return shape, agree, struct, ref_std


def _bins_as_arrays(bins: dict) -> tuple[np.ndarray, np.ndarray]:
    offs = np.fromiter(bins.keys(), dtype=np.int64, count=len(bins))
    counts = np.fromiter(bins.values(), dtype=np.int64, count=len(bins))
    return offs, counts


def _significance(
    offs: np.ndarray, counts: np.ndarray, off: int, ref_frames: int, q_frames: int
) -> float:
    """How far the winning offset bin stands above the chance background.

    The background must be measured *away* from the candidate: a genuine occurrence of a
    slowly-varying creative deposits votes not only in its own bin but in a triangular
    skirt +/- ``ref_frames`` wide around it (partial self-overlap).  Averaging over every
    non-empty bin therefore averages over the signal itself and, for a near-stationary
    creative, caps the achievable ratio at about 2 -- which rejects perfect matches.
    Excluding the skirt makes the statistic mean what its name says.
    """
    if counts.size == 0:
        return 0.0
    near = np.abs(offs - off) <= ref_frames
    outside_votes = float(counts[~near].sum())
    n_offsets = max(q_frames + ref_frames, 1)
    n_outside = max(n_offsets - (2 * ref_frames + 1), 1)
    background = outside_votes / n_outside
    return float(counts[offs == off].max()) / (background + 1.0)


# --------------------------------------------------------------------------------------
# matches
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """One detected occurrence of a known creative inside an episode."""

    creative_id: str
    start: float
    end: float
    confidence: float
    votes: int = 0
    vote_ratio: float = 0.0
    coverage: float = 0.0
    shape: float = 0.0
    agree: float = 0.0
    struct: Optional[float] = None
    offset_frames: int = 0

    def as_interval(self) -> list[float]:
        return [self.start, self.end]

    def __iter__(self):
        """Unpack as ``(creative_id, start, end, confidence)`` per the step 4 contract."""
        yield from (self.creative_id, self.start, self.end, self.confidence)


# --------------------------------------------------------------------------------------
# SQLite storage
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fp_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fp_creatives (
    creative_id   TEXT PRIMARY KEY,
    source        TEXT,
    algo_version  INTEGER NOT NULL,
    params_key    BLOB    NOT NULL,
    n_samples     INTEGER NOT NULL,
    n_frames      INTEGER NOT NULL,
    n_hashes      INTEGER NOT NULL,
    fingerprint   BLOB    NOT NULL,
    confirmations INTEGER NOT NULL DEFAULT 1,
    first_seen    REAL    NOT NULL,
    last_seen     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS fp_hashes (
    hash        INTEGER NOT NULL,
    creative_id TEXT    NOT NULL REFERENCES fp_creatives(creative_id) ON DELETE CASCADE,
    frame       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fp_hashes_hash ON fp_hashes (hash);
CREATE INDEX IF NOT EXISTS idx_fp_hashes_creative ON fp_hashes (creative_id);
"""

try:  # pragma: no cover - exercised implicitly
    from ..core.db import connect as _connect
except Exception:  # pragma: no cover - keeps step 4 standalone if step 2 moves

    def _connect(path, **kwargs):  # type: ignore[misc]
        conn = sqlite3.connect(os.fspath(path), timeout=30.0, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


class FingerprintLibrary:
    """A SQLite-backed index of confirmed ad creatives.

    ``db_path`` may be ``":memory:"``.  The tables are namespaced ``fp_*`` so the
    library can share the step 2 pipeline database if you want it to.
    """

    def __init__(
        self,
        db_path: "str | os.PathLike[str]" = ":memory:",
        *,
        params: FingerprintParams = DEFAULT_PARAMS,
        thresholds: MatchThresholds = DEFAULT_THRESHOLDS,
        now_fn: Callable[[], float] = time.time,
    ):
        self.params = params
        self.thresholds = thresholds
        self._now = now_fn
        path = os.fspath(db_path)
        if path == ":memory:":
            self.conn = sqlite3.connect(":memory:", isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
        else:
            self.conn = _connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT INTO fp_meta (key, value) VALUES ('algo_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(ALGO_VERSION),),
        )
        self._mel_cache: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "FingerprintLibrary":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ writing

    def add_creative(
        self,
        creative_id: str,
        audio: "str | os.PathLike[str] | np.ndarray | Fingerprint",
        *,
        source: Optional[str] = None,
        replace_existing: bool = False,
    ) -> Fingerprint:
        """Add (or replace) a confirmed creative.  This is the *confirmation* hook.

        Called when something upstream -- a human, or the LLM tier -- has decided that a
        span really is an ad.  Every later episode containing it is then a lookup.
        """
        if not creative_id:
            raise FingerprintError("creative_id must be a non-empty string")
        exists = self.has_creative(creative_id)
        if exists and not replace_existing:
            raise FingerprintError(f"creative {creative_id!r} is already in the library")

        fp = _as_fingerprint(audio, self.params)
        if fp.n_frames == 0:
            raise FingerprintError(f"creative {creative_id!r} has no usable audio")
        if fp.pairs.shape[0] == 0:
            raise FingerprintError(f"creative {creative_id!r} produced no landmarks")

        now = float(self._now())
        if source is None and isinstance(audio, (str, os.PathLike)):
            source = os.fspath(audio)

        cur = self.conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("DELETE FROM fp_hashes WHERE creative_id = ?", (creative_id,))
            cur.execute("DELETE FROM fp_creatives WHERE creative_id = ?", (creative_id,))
            cur.execute(
                "INSERT INTO fp_creatives (creative_id, source, algo_version, params_key,"
                " n_samples, n_frames, n_hashes, fingerprint, confirmations,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    creative_id,
                    source,
                    ALGO_VERSION,
                    self.params.key(),
                    fp.n_samples,
                    fp.n_frames,
                    int(fp.pairs.shape[0]),
                    fp.to_bytes(),
                    1,
                    now,
                    now,
                ),
            )
            cur.executemany(
                "INSERT INTO fp_hashes (hash, creative_id, frame) VALUES (?,?,?)",
                ((int(h), creative_id, int(t)) for h, t in fp.pairs),
            )
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        else:
            cur.execute("COMMIT")
        self._mel_cache.pop(creative_id, None)
        return fp

    def confirm(self, creative_id: str) -> int:
        """Record another confirmed sighting.  Returns the new confirmation count."""
        row = self.conn.execute(
            "SELECT confirmations FROM fp_creatives WHERE creative_id = ?", (creative_id,)
        ).fetchone()
        if row is None:
            raise FingerprintError(f"unknown creative {creative_id!r}")
        n = int(row[0]) + 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE fp_creatives SET confirmations = ?, last_seen = ? WHERE creative_id = ?",
                (n, float(self._now()), creative_id),
            )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        return n

    def remove_creative(self, creative_id: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM fp_hashes WHERE creative_id = ?", (creative_id,))
            self.conn.execute("DELETE FROM fp_creatives WHERE creative_id = ?", (creative_id,))
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        self._mel_cache.pop(creative_id, None)

    # ------------------------------------------------------------------ reading

    def has_creative(self, creative_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM fp_creatives WHERE creative_id = ?", (creative_id,)
            ).fetchone()
            is not None
        )

    def creative_ids(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT creative_id FROM fp_creatives ORDER BY creative_id"
            )
        ]

    def __len__(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM fp_creatives").fetchone()[0])

    def get_fingerprint(self, creative_id: str) -> Fingerprint:
        row = self.conn.execute(
            "SELECT fingerprint, algo_version FROM fp_creatives WHERE creative_id = ?",
            (creative_id,),
        ).fetchone()
        if row is None:
            raise FingerprintError(f"unknown creative {creative_id!r}")
        if int(row[1]) != ALGO_VERSION:
            raise FingerprintError(
                f"creative {creative_id!r} was fingerprinted by algo v{row[1]}, "
                f"this is v{ALGO_VERSION}"
            )
        return Fingerprint.from_bytes(bytes(row[0]), self.params)

    def _ref_mel(self, creative_id: str) -> np.ndarray:
        mel = self._mel_cache.get(creative_id)
        if mel is None:
            mel = self.get_fingerprint(creative_id).mel
            self._mel_cache[creative_id] = mel
        return mel

    # ------------------------------------------------------------------ matching

    def _vote_histogram(self, positions: np.ndarray, n_phases: int) -> dict[str, dict[int, int]]:
        """``{creative_id: {offset_sub_frames: votes}}`` from a SQL join on the postings.

        Offsets are in sub-frame units of ``hop / n_phases`` samples: query positions
        already are, and reference frames are scaled by ``n_phases`` in the join.
        """
        conn = self.conn
        conn.execute("DROP TABLE IF EXISTS temp.fp_query")
        conn.execute("CREATE TEMP TABLE fp_query (hash INTEGER NOT NULL, pos INTEGER NOT NULL)")
        conn.executemany(
            "INSERT INTO temp.fp_query (hash, pos) VALUES (?,?)",
            ((int(h), int(t)) for h, t in positions),
        )
        rows = conn.execute(
            "SELECT h.creative_id AS cid, (q.pos - ? * h.frame) AS off, COUNT(*) AS votes "
            "FROM temp.fp_query q JOIN fp_hashes h ON h.hash = q.hash "
            "GROUP BY cid, off",
            (int(n_phases),),
        ).fetchall()
        conn.execute("DROP TABLE IF EXISTS temp.fp_query")
        hist: dict[str, dict[int, int]] = {}
        for cid, off, votes in rows:
            hist.setdefault(cid, {})[int(off)] = int(votes)
        return hist

    def match(
        self,
        episode_audio: "str | os.PathLike[str] | np.ndarray | Fingerprint",
        *,
        thresholds: Optional[MatchThresholds] = None,
    ) -> list[Match]:
        """Find known creatives inside ``episode_audio``.

        Returns ``(creative_id, start, end, confidence)`` records -- :class:`Match`
        unpacks as exactly that tuple -- sorted by start time, non-overlapping.
        Returns ``[]`` when nothing clears the thresholds, which is the required answer
        for ad-free material (contract S4.3).
        """
        th = thresholds or self.thresholds
        if len(self) == 0:
            return []
        qfp = _as_fingerprint(episode_audio, self.params, n_phases=VERIFY_PHASES)
        if qfp.pairs.shape[0] == 0:
            return []

        k = qfp.n_phases
        hist = self._vote_histogram(qfp.query_positions(), k)
        totals = {
            r[0]: int(r[1])
            for r in self.conn.execute("SELECT creative_id, n_hashes FROM fp_creatives")
        }

        candidates: list[Match] = []
        q_sub = qfp.mel.shape[0] * k
        for cid, bins in hist.items():
            ref_mel = self._ref_mel(cid)
            ref_sub = ref_mel.shape[0] * k
            n_ref_hashes = max(totals.get(cid, 1), 1)
            offs, counts = _bins_as_arrays(bins)

            # Only offsets that both clear the absolute floor and stand out from the
            # background are worth verifying.
            ranked = sorted(bins.items(), key=lambda kv: (-kv[1], kv[0]))
            for off, votes in ranked[: th.max_candidates]:
                if votes < th.min_votes:
                    break
                ratio = _significance(offs, counts, int(off), ref_sub, q_sub)
                coverage = votes / n_ref_hashes
                if ratio < th.min_vote_ratio or coverage < th.min_vote_coverage:
                    continue
                cand = self._verify(
                    cid, ref_mel, qfp, int(off) * qfp.sub_hop, th, votes, ratio, coverage
                )
                if cand is not None:
                    candidates.append(cand)

        # greedy non-maximum suppression by confidence, then chronological
        chosen: list[Match] = []
        for cand in sorted(candidates, key=lambda m: (-m.confidence, m.start)):
            if any(cand.start < c.end and c.start < cand.end for c in chosen):
                continue
            chosen.append(cand)
        return sorted(chosen, key=lambda m: (m.start, m.creative_id))

    def _verify(
        self,
        creative_id: str,
        ref_mel: np.ndarray,
        qfp: Fingerprint,
        offset_samples: int,
        th: MatchThresholds,
        votes: int,
        ratio: float,
        coverage: float,
    ) -> Optional[Match]:
        """Second stage: does the audio at ``offset_samples`` look like the creative?

        Searches a small grid of (integer frame delta) x (sub-hop phase).  The phase
        search is not cosmetic: a splice that is half a hop off the analysis grid drops
        the measured similarity of a *correct* match well below the level an impostor
        reaches, so without it no single threshold can satisfy both S4.1 and S4.3.
        """
        params = self.params
        ref_frames = ref_mel.shape[0]

        best = None  # (confidence, start_sample, shape, agree, struct)
        for phase_offset, qmel in qfp.mel_views():
            q_frames = qmel.shape[0]
            # Convert the candidate's absolute sample position onto this phase's grid.
            base = (offset_samples - phase_offset) / params.hop
            for delta in (-2, -1, 0, 1, 2):
                start = int(round(base)) + delta
                lo = max(start, 0)
                hi = min(start + ref_frames, q_frames)
                if hi - lo <= 0:
                    continue
                if (hi - lo) / ref_frames < th.min_overlap_fraction:
                    continue
                if params.frames_to_seconds(hi - lo) < th.min_match_seconds:
                    continue
                shape, agree, struct, ref_std = _shape_struct(
                    ref_mel[lo - start : hi - start], qmel[lo:hi]
                )
                if ref_std < th.struct_floor:
                    # A stationary reference carries no temporal structure; the
                    # structural test would be reading numerical noise, so it is skipped
                    # and reported as None rather than silently passed.
                    confidence = min(shape, agree)
                    struct_reported: Optional[float] = None
                else:
                    confidence = min(
                        shape, agree, max(struct if struct is not None else 0.0, 0.0)
                    )
                    struct_reported = struct
                start_sample = start * params.hop + phase_offset
                if best is None or confidence > best[0]:
                    best = (confidence, start_sample, shape, agree, struct_reported)

        if best is None:
            return None
        confidence, start_sample, shape, agree, struct = best
        if shape < th.min_shape:
            return None
        if agree < th.min_frame_agreement:
            return None
        if struct is not None and struct < th.min_struct:
            return None
        if confidence < th.min_confidence:
            return None

        start_s = start_sample / params.sample_rate
        end_s = start_s + ref_frames * params.hop / params.sample_rate
        return Match(
            creative_id=creative_id,
            start=max(start_s, 0.0),
            end=end_s,
            confidence=float(confidence),
            votes=int(votes),
            vote_ratio=float(ratio),
            coverage=float(coverage),
            shape=float(shape),
            agree=float(agree),
            struct=struct,
            offset_frames=int(round(start_sample / params.hop)),
        )

    def explain(
        self,
        episode_audio: "str | os.PathLike[str] | np.ndarray | Fingerprint",
    ) -> list[dict]:
        """Diagnostics for every creative: the best offset and its raw scores.

        Used by the tests to report the actual separation between true matches and the
        nearest miss, rather than only asserting a boolean.
        """
        qfp = _as_fingerprint(episode_audio, self.params, n_phases=VERIFY_PHASES)
        k = qfp.n_phases
        hist = (
            self._vote_histogram(qfp.query_positions(), k) if qfp.pairs.shape[0] else {}
        )
        totals = {
            r[0]: int(r[1])
            for r in self.conn.execute("SELECT creative_id, n_hashes FROM fp_creatives")
        }
        out: list[dict] = []
        for cid in self.creative_ids():
            bins = hist.get(cid, {})
            if not bins:
                out.append(
                    {
                        "creative_id": cid,
                        "votes": 0,
                        "vote_ratio": 0.0,
                        "coverage": 0.0,
                        "shape": 0.0,
                        "agree": 0.0,
                        "struct": None,
                        "confidence": 0.0,
                        "offset_frames": None,
                    }
                )
                continue
            offs, counts = _bins_as_arrays(bins)
            off, votes = max(bins.items(), key=lambda kv: (kv[1], -kv[0]))
            ref_mel = self._ref_mel(cid)
            significance = _significance(
                offs, counts, int(off), ref_mel.shape[0] * k, qfp.mel.shape[0] * k
            )
            m = self._verify(
                cid,
                ref_mel,
                qfp,
                int(off) * qfp.sub_hop,
                replace(
                    self.thresholds,
                    min_shape=-1.0,
                    min_frame_agreement=-1.0,
                    min_struct=-2.0,
                    min_confidence=-2.0,
                    min_overlap_fraction=0.0,
                    min_match_seconds=0.0,
                ),
                votes,
                significance,
                votes / max(totals.get(cid, 1), 1),
            )
            out.append(
                {
                    "creative_id": cid,
                    "votes": int(votes),
                    "vote_ratio": significance,
                    "coverage": votes / max(totals.get(cid, 1), 1),
                    "shape": m.shape if m else 0.0,
                    "agree": m.agree if m else 0.0,
                    "struct": m.struct if m else None,
                    "confidence": m.confidence if m else 0.0,
                    "offset_frames": int(off),
                }
            )
        return out


# --------------------------------------------------------------------------------------
# the tier: fingerprint first, LLM only on a miss
# --------------------------------------------------------------------------------------


@dataclass
class TierResult:
    """What :meth:`FingerprintTier.analyze` decided, and how it got there."""

    ads: list[Match]
    from_fingerprint: bool
    llm_calls: int
    escalated: bool


class FingerprintTier:
    """Fingerprint-first ad detection with an escalation hook.

    ``llm`` is any callable ``(audio_path) -> iterable of (creative_id, start, end)``.
    It is invoked **only** when the fingerprint library has nothing for the episode --
    that is the compounding property the contract's S4.4 asserts on.  Whatever the LLM
    returns is treated as confirmed and added to the library, so the *next* episode
    carrying the same creative never reaches the LLM.
    """

    def __init__(
        self,
        library: FingerprintLibrary,
        llm: Optional[Callable[[str], Iterable[Sequence]]] = None,
        *,
        thresholds: Optional[MatchThresholds] = None,
    ):
        self.library = library
        self.llm = llm
        self.thresholds = thresholds or library.thresholds

    def analyze(self, episode_path: "str | os.PathLike[str]") -> TierResult:
        matches = self.library.match(episode_path, thresholds=self.thresholds)
        if matches:
            for m in matches:
                self.library.confirm(m.creative_id)
            return TierResult(ads=matches, from_fingerprint=True, llm_calls=0, escalated=False)

        if self.llm is None:
            return TierResult(ads=[], from_fingerprint=False, llm_calls=0, escalated=False)

        proposals = list(self.llm(os.fspath(episode_path)))
        confirmed = self._learn(episode_path, proposals)
        return TierResult(ads=confirmed, from_fingerprint=False, llm_calls=1, escalated=True)

    def _learn(
        self, episode_path: "str | os.PathLike[str]", proposals: Iterable[Sequence]
    ) -> list[Match]:
        """Cut each confirmed span out of the episode and store it as a creative."""
        params = self.library.params
        samples = decode_audio(episode_path, params.sample_rate)
        out: list[Match] = []
        for creative_id, start, end in proposals:
            a = max(int(round(float(start) * params.sample_rate)), 0)
            b = min(int(round(float(end) * params.sample_rate)), samples.size)
            if b - a < params.n_fft:
                continue
            if not self.library.has_creative(creative_id):
                self.library.add_creative(
                    creative_id, samples[a:b], source=f"llm:{os.fspath(episode_path)}"
                )
            out.append(
                Match(
                    creative_id=str(creative_id),
                    start=a / params.sample_rate,
                    end=b / params.sample_rate,
                    confidence=1.0,
                )
            )
        return out
