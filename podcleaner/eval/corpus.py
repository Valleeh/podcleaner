"""Deterministic synthetic ad corpus.

The ground truth here is exact **because we constructed it**: we synthesise content
audio, synthesise a small pool of distinct "ad creatives", splice the creatives into the
content at offsets we chose, and write a manifest recording those exact boundaries.
Nobody judged anything, so there is nothing to disagree with.

Design notes
------------

* **Sample-exact.** Everything is 16 kHz mono PCM WAV and every part duration is an
  exact whole number of samples, so concatenation is lossless and boundaries land on
  sample edges.  ``ffprobe`` -- *not* our own arithmetic -- is used by the tests as the
  independent oracle for durations.
* **Deterministic.** Same ``seed`` -> byte-identical audio and identical manifest.  The
  generator seeds ``random.Random`` explicitly and passes fixed seeds to ffmpeg's
  ``anoisesrc``; no wall-clock or ``os.urandom`` input anywhere.
* **Negative controls included.** Some episodes contain *no* ads at all
  (``ads: []``), so a detector that says "yes" to everything can be caught.
* **Offline.** Nothing is downloaded.  ffmpeg's ``lavfi`` synthesises every sample.

Manifest layout (``manifest.json``)::

    {
      "schema_version": 1,
      "generator": "podcleaner.eval.corpus",
      "seed": 1234,
      "sample_rate": 16000,
      "creatives": [ {"id", "path", "duration_seconds", "duration_samples", "spec"} ],
      "episodes": [
        {
          "id": "ep000",
          "path": "episodes/ep000.wav",
          "duration_seconds": 42.5,
          "duration_samples": 680000,
          "ads": [ {"start", "end", "creative_id", "creative_path"} ],
          "parts": [ {"kind", "source_id", "start", "end",
                      "duration_seconds", "duration_samples"} ]
        }
      ]
    }

``ads`` is exactly the gold interval list expected by
:func:`podcleaner.eval.scoring.score`.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

__all__ = [
    "AD_CREATIVES",
    "CONTENT_VOICES",
    "DEFAULT_SEED",
    "SAMPLE_RATE",
    "CorpusError",
    "Part",
    "ffprobe_duration_seconds",
    "ffprobe_sample_count",
    "generate_corpus",
    "gold_intervals",
    "load_manifest",
]

SAMPLE_RATE = 16_000
"""All corpus audio is 16 kHz mono PCM.  One frame = 1/16000 s."""

FRAME_SECONDS = 1.0 / SAMPLE_RATE

DEFAULT_SEED = 1234

MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1


class CorpusError(RuntimeError):
    """Raised when corpus generation cannot proceed (missing ffmpeg, bad ffprobe output)."""


# --------------------------------------------------------------------------------------
# source material: everything is synthesised by ffmpeg's lavfi, nothing is downloaded
# --------------------------------------------------------------------------------------

#: "Ad creatives".  Deliberately spectrally distinct from the content voices so that a
#: fingerprinter (step 4) has something separable to find.
AD_CREATIVES: Sequence[Dict[str, object]] = (
    {
        "id": "ad_sweep_lo",
        "duration_seconds": 8.0,
        # A rising sine sweep -- a very distinctive fingerprint.
        "lavfi": "sine=frequency=300:beep_factor=4:sample_rate={sr}:duration={dur}",
    },
    {
        "id": "ad_dualtone",
        "duration_seconds": 6.5,
        "lavfi": (
            "sine=frequency=1200:sample_rate={sr}:duration={dur}[t1];"
            "sine=frequency=1700:sample_rate={sr}:duration={dur}[t2];"
            "[t1][t2]amix=inputs=2:duration=longest"
        ),
    },
    {
        "id": "ad_noise_burst",
        "duration_seconds": 5.0,
        "lavfi": "anoisesrc=color=violet:sample_rate={sr}:duration={dur}:seed=90210",
    },
)

#: "Content" material.  Low, speech-ish frequencies plus brown noise.
CONTENT_VOICES: Sequence[Dict[str, object]] = (
    {"id": "content_a", "lavfi": "sine=frequency=140:sample_rate={sr}:duration={dur}"},
    {"id": "content_b", "lavfi": "sine=frequency=190:sample_rate={sr}:duration={dur}"},
    {
        "id": "content_c",
        "lavfi": "anoisesrc=color=brown:sample_rate={sr}:duration={dur}:seed=4242",
    },
)


# --------------------------------------------------------------------------------------
# ffmpeg / ffprobe helpers.  ffprobe is used only as an *oracle*, never to build.
# --------------------------------------------------------------------------------------


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise CorpusError(f"{name} not found on PATH; the corpus generator needs it")
    return path


def _run(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CorpusError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc.stdout


def ffprobe_duration_seconds(path: Path | str) -> float:
    """Container duration in seconds, straight from ffprobe.

    This is the *independent* oracle: it does not share code with the splicer.
    """
    out = _run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    ).strip()
    try:
        return float(out)
    except ValueError as exc:
        raise CorpusError(f"ffprobe returned non-numeric duration {out!r}") from exc


def ffprobe_sample_count(path: Path | str) -> int:
    """Number of audio samples in the first audio stream, per ffprobe.

    Stricter than :func:`ffprobe_duration_seconds` -- for PCM this is exact, so a
    one-sample splicing error is visible.
    """
    out = _run(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration_ts",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    ).strip()
    try:
        return int(out)
    except ValueError as exc:
        raise CorpusError(f"ffprobe returned non-integer duration_ts {out!r}") from exc


def _synthesize(lavfi: str, duration_seconds: float, dest: Path) -> None:
    """Render one lavfi graph to a mono 16-bit PCM WAV of exactly ``duration_seconds``."""
    graph = lavfi.format(sr=SAMPLE_RATE, dur=f"{duration_seconds:.6f}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            graph,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-t",
            f"{duration_seconds:.6f}",
            # keep the output byte-reproducible: no encoder tag, no creation time
            "-fflags",
            "+bitexact",
            "-flags",
            "+bitexact",
            "-map_metadata",
            "-1",
            str(dest),
        ]
    )


def _concat(parts: Sequence[Path], dest: Path, workdir: Path) -> None:
    """Losslessly concatenate PCM WAV ``parts`` into ``dest``."""
    listing = workdir / f"{dest.stem}.concat.txt"
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-fflags",
            "+bitexact",
            "-flags",
            "+bitexact",
            "-map_metadata",
            "-1",
            str(dest),
        ]
    )
    listing.unlink()


# --------------------------------------------------------------------------------------
# planning: durations are chosen in whole samples so nothing can drift
# --------------------------------------------------------------------------------------


@dataclass
class Part:
    """One contiguous chunk of an episode timeline."""

    kind: str  # "content" or "ad"
    source_id: str
    duration_samples: int
    start: float = 0.0
    end: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return self.duration_samples / SAMPLE_RATE


def _samples(seconds: float) -> int:
    """Seconds -> whole samples.  Rounding here is the *only* place rounding happens."""
    return int(round(seconds * SAMPLE_RATE))


def _total_duration_samples(parts: Sequence[Part]) -> int:
    """Total length of an episode, as the sum of its parts.

    Mutation (d) in the verification contract is an off-by-one here.  ffprobe, which
    knows nothing about this function, is what catches it.
    """
    return sum(part.duration_samples for part in parts)


def _lay_out(parts: Sequence[Part]) -> None:
    """Assign absolute ``start``/``end`` seconds to each part by cumulative sum."""
    cursor_samples = 0
    for part in parts:
        part.start = cursor_samples / SAMPLE_RATE
        cursor_samples += part.duration_samples
        part.end = cursor_samples / SAMPLE_RATE


def _plan_episode(rng: random.Random, n_ads: int) -> List[Part]:
    """Interleave ``n_ads`` ad breaks between ``n_ads + 1`` content chunks."""
    parts: List[Part] = []
    for slot in range(n_ads + 1):
        voice = CONTENT_VOICES[rng.randrange(len(CONTENT_VOICES))]
        # tenths of a second keeps durations exactly representable in samples
        seconds = rng.randrange(70, 220) / 10.0
        parts.append(
            Part(
                kind="content",
                source_id=str(voice["id"]),
                duration_samples=_samples(seconds),
            )
        )
        if slot < n_ads:
            creative = AD_CREATIVES[rng.randrange(len(AD_CREATIVES))]
            parts.append(
                Part(
                    kind="ad",
                    source_id=str(creative["id"]),
                    duration_samples=_samples(float(creative["duration_seconds"])),
                )
            )
    return parts


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------


@dataclass
class _EpisodeRecord:
    id: str
    path: str
    duration_seconds: float
    duration_samples: int
    ads: List[Dict[str, object]] = field(default_factory=list)
    parts: List[Dict[str, object]] = field(default_factory=list)


def generate_corpus(
    out_dir: Path | str,
    *,
    seed: int = DEFAULT_SEED,
    n_episodes: int = 6,
    n_ad_free_episodes: int = 2,
    max_ads_per_episode: int = 3,
) -> dict:
    """Generate a synthetic corpus under ``out_dir`` and return its manifest.

    ``n_ad_free_episodes`` of the ``n_episodes`` contain no ads at all -- these are the
    negative controls (a detector that fires on them is producing false cuts on material
    where the correct answer is "nothing").

    The manifest is also written to ``out_dir/manifest.json``.
    """
    if n_episodes < 1:
        raise CorpusError("n_episodes must be >= 1")
    if not 0 <= n_ad_free_episodes <= n_episodes:
        raise CorpusError("n_ad_free_episodes must be between 0 and n_episodes")
    if max_ads_per_episode < 1:
        raise CorpusError("max_ads_per_episode must be >= 1")

    out_path = Path(out_dir)
    if out_path.exists():
        shutil.rmtree(out_path)
    creatives_dir = out_path / "creatives"
    episodes_dir = out_path / "episodes"
    work_dir = out_path / ".work"
    for d in (creatives_dir, episodes_dir, work_dir):
        d.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)

    # --- creatives: rendered once, reused across episodes (step 4 needs them standalone)
    creative_records: List[Dict[str, object]] = []
    creative_paths: Dict[str, Path] = {}
    for creative in AD_CREATIVES:
        cid = str(creative["id"])
        dest = creatives_dir / f"{cid}.wav"
        duration = float(creative["duration_seconds"])
        _synthesize(str(creative["lavfi"]), duration, dest)
        creative_paths[cid] = dest
        creative_records.append(
            {
                "id": cid,
                "path": dest.relative_to(out_path).as_posix(),
                "duration_seconds": duration,
                "duration_samples": _samples(duration),
                "spec": str(creative["lavfi"]),
            }
        )

    # --- which episodes are ad-free.  Chosen deterministically from the seed.
    ad_free = set(rng.sample(range(n_episodes), n_ad_free_episodes))

    episodes: List[Dict[str, object]] = []
    for index in range(n_episodes):
        ep_id = f"ep{index:03d}"
        n_ads = 0 if index in ad_free else rng.randrange(1, max_ads_per_episode + 1)
        parts = _plan_episode(rng, n_ads)
        _lay_out(parts)

        # render each part, reusing the already-rendered creative for ad parts
        rendered: List[Path] = []
        for part_index, part in enumerate(parts):
            if part.kind == "ad":
                rendered.append(creative_paths[part.source_id])
                continue
            voice = next(v for v in CONTENT_VOICES if v["id"] == part.source_id)
            chunk = work_dir / f"{ep_id}_{part_index:02d}_{part.source_id}.wav"
            _synthesize(str(voice["lavfi"]), part.duration_seconds, chunk)
            rendered.append(chunk)

        episode_path = episodes_dir / f"{ep_id}.wav"
        _concat(rendered, episode_path, work_dir)

        total_samples = _total_duration_samples(parts)
        episodes.append(
            {
                "id": ep_id,
                "path": episode_path.relative_to(out_path).as_posix(),
                "duration_seconds": total_samples / SAMPLE_RATE,
                "duration_samples": total_samples,
                "ads": [
                    {
                        "start": part.start,
                        "end": part.end,
                        "creative_id": part.source_id,
                        "creative_path": creative_paths[part.source_id]
                        .relative_to(out_path)
                        .as_posix(),
                    }
                    for part in parts
                    if part.kind == "ad"
                ],
                "parts": [
                    {
                        "kind": part.kind,
                        "source_id": part.source_id,
                        "start": part.start,
                        "end": part.end,
                        "duration_seconds": part.duration_seconds,
                        "duration_samples": part.duration_samples,
                    }
                    for part in parts
                ],
            }
        )

    shutil.rmtree(work_dir)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator": "podcleaner.eval.corpus",
        "seed": seed,
        "sample_rate": SAMPLE_RATE,
        "ground_truth": "exact-by-construction",
        "creatives": creative_records,
        "episodes": episodes,
    }
    (out_path / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_manifest(corpus_dir: Path | str) -> dict:
    """Read the manifest written by :func:`generate_corpus`."""
    return json.loads((Path(corpus_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))


def gold_intervals(episode: dict) -> List[List[float]]:
    """Gold ad intervals for one manifest episode, ready for ``scoring.score``."""
    return [[ad["start"], ad["end"]] for ad in episode["ads"]]


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m podcleaner.eval.corpus",
        description="Generate the deterministic synthetic ad corpus.",
    )
    parser.add_argument("--out", default="corpus/synthetic", help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--episodes", type=int, default=6)
    parser.add_argument("--ad-free", type=int, default=2)
    args = parser.parse_args(argv)

    manifest = generate_corpus(
        args.out,
        seed=args.seed,
        n_episodes=args.episodes,
        n_ad_free_episodes=args.ad_free,
    )
    total_ads = sum(len(ep["ads"]) for ep in manifest["episodes"])
    print(
        f"wrote {len(manifest['episodes'])} episodes "
        f"({total_ads} ad insertions) to {args.out}/{MANIFEST_NAME}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
