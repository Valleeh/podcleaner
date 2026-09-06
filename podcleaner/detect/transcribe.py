"""Transcription: whisper.cpp behind a small interface.

The production image (see ``docs/architecture.md`` §7.2) has ``whisper-cli`` and
``ffmpeg`` on ``PATH``.  On the development host neither is installed natively; both
live in the ``whisper-cpp:local`` Docker image, so the same commands are run inside a
throwaway container.  :class:`WhisperConfig.docker_image` picks the mode.

Design points:

* **Windows are cut with ffmpeg**, not with whisper's own ``--offset-t``, because
  ffmpeg's input seek is instant while whisper would decode the whole file first.  The
  window start is added back to every timestamp, so a transcript of a window is in the
  timeline of the whole episode.
* **Word timestamps come from whisper.cpp tokens** (``-ojf``), grouped into words by
  :mod:`podcleaner.transcripts`.  They are approximate (tens of milliseconds) and are
  used for evaluation and anchor search, never as cut points.
* **Greedy decoding by default** (``-bo 1 -bs 1``).  On this CPU beam search was
  measured at about 3x slower than real time; greedy runs at roughly 0.65x real time
  with ``small`` and is the configuration the reference transcripts were made with.
* **Results are cached** by content hash plus every parameter that changes the output,
  so a test that re-reads a window never pays for it twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from podcleaner.logging import get_logger
from podcleaner.transcripts import Transcript, parse_whisper_json

__all__ = [
    "DEFAULT_MODEL_DIR",
    "TranscriptionError",
    "WhisperConfig",
    "WhisperCppTranscriber",
    "file_sha256",
]

logger = get_logger(__name__)

PathLike = Union[str, Path]

DEFAULT_MODEL_DIR = Path("/opt/whisper/models")


class TranscriptionError(RuntimeError):
    """Raised when audio cannot be transcribed (missing tool, failed process, bad output)."""


_sha_cache: Dict[Tuple[str, int, int], str] = {}


def file_sha256(path: PathLike) -> str:
    """SHA-256 of a file, memoised per (path, size, mtime) for the life of the process."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if key in _sha_cache:
        return _sha_cache[key]
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


@dataclass(frozen=True)
class WhisperConfig:
    #: GGML model file.  ``small-q5_1`` is the largest that fits the 2 GB host.
    model_path: Path = DEFAULT_MODEL_DIR / "ggml-small-q5_1.bin"
    #: Docker image providing ``whisper-cli`` and ``ffmpeg``; ``None`` runs them from PATH.
    docker_image: Optional[str] = "whisper-cpp:local"
    threads: int = 4
    #: ``1``/``1`` is greedy decoding.  whisper-cli's defaults are 5/5 (beam search).
    best_of: int = 1
    beam_size: int = 1
    #: ISO 639-1 code or ``None`` for auto-detection.
    language: Optional[str] = None
    memory_limit: str = "1200m"
    #: Extra whisper-cli arguments, appended verbatim.
    extra_args: Tuple[str, ...] = ()
    docker: str = "docker"
    whisper_cli: str = "whisper-cli"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    @property
    def model_name(self) -> str:
        return Path(self.model_path).stem.replace("ggml-", "")

    @property
    def decoding(self) -> str:
        return "greedy" if self.best_of == 1 and self.beam_size == 1 else f"beam{self.beam_size}-bo{self.best_of}"

    @property
    def label(self) -> str:
        return f"whisper.cpp/{self.model_name}/{self.decoding}"

    def cache_fields(self) -> dict:
        return {
            "model": Path(self.model_path).name,
            "best_of": self.best_of,
            "beam_size": self.beam_size,
            "language": self.language,
            "extra_args": list(self.extra_args),
        }


Runner = Callable[[str, Dict[Path, Tuple[str, bool]]], subprocess.CompletedProcess]


class WhisperCppTranscriber:
    """Transcribe audio (or a window of it) with whisper.cpp.

    ``runner`` can be injected for tests: it receives a bash script and a mapping of
    host directories to ``(container_path, read_only)`` and returns a
    :class:`subprocess.CompletedProcess`.
    """

    def __init__(
        self,
        config: WhisperConfig = WhisperConfig(),
        *,
        cache_dir: Optional[PathLike] = None,
        runner: Optional[Runner] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None
        self._runner = runner
        self.timeout = timeout
        self.calls = 0

    # -- availability ---------------------------------------------------------------------

    def availability(self) -> Tuple[bool, str]:
        """``(ok, reason)`` -- whether this host can run the configured pipeline."""
        cfg = self.config
        if not Path(cfg.model_path).exists():
            return False, f"model not found: {cfg.model_path}"
        if self._runner is not None:
            return True, "custom runner"
        if cfg.docker_image:
            if shutil.which(cfg.docker) is None:
                return False, "docker not on PATH"
            proc = subprocess.run(
                [cfg.docker, "image", "inspect", cfg.docker_image],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return False, f"docker image {cfg.docker_image} not present"
            return True, f"docker image {cfg.docker_image}"
        for tool in (cfg.whisper_cli, cfg.ffmpeg, cfg.ffprobe):
            if shutil.which(tool) is None:
                return False, f"{tool} not on PATH"
        return True, "local binaries"

    # -- probing ------------------------------------------------------------------------

    def probe_duration(self, audio: PathLike) -> float:
        """Duration in seconds according to ffprobe."""
        src = Path(audio).resolve()
        script = (
            f"{self.config.ffprobe} -v error -show_entries format=duration "
            f"-of default=nw=1:nk=1 /in/{shlex.quote(src.name)}"
        )
        proc = self._run(script, {src.parent: ("/in", True)})
        try:
            return float(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise TranscriptionError(f"ffprobe gave no duration for {src}: {proc.stdout!r} {proc.stderr!r}") from exc

    # -- transcription --------------------------------------------------------------------

    def transcribe(
        self,
        audio: PathLike,
        *,
        start: float = 0.0,
        duration: Optional[float] = None,
        language: Optional[str] = None,
        use_cache: bool = True,
    ) -> Transcript:
        """Transcribe ``audio`` from ``start`` for ``duration`` seconds (or to the end).

        Timestamps in the result are in the timeline of the whole file.
        """
        src = Path(audio).resolve()
        if not src.exists():
            raise TranscriptionError(f"no such audio file: {src}")
        if start < 0:
            raise TranscriptionError(f"start must be >= 0, got {start}")
        if duration is not None and duration <= 0:
            raise TranscriptionError(f"duration must be > 0, got {duration}")
        lang = language or self.config.language
        cfg = self.config

        cache_key = None
        if self.cache_dir is not None and use_cache:
            key_doc = {
                "sha256": file_sha256(src),
                "start": round(start, 3),
                "duration": None if duration is None else round(duration, 3),
                "language": lang,
                **cfg.cache_fields(),
                "format": 2,
            }
            cache_key = hashlib.sha256(json.dumps(key_doc, sort_keys=True).encode()).hexdigest()[:24]
            cached = self.cache_dir / "whisper" / f"{cache_key}.json"
            if cached.exists():
                logger.info("transcription_cached", audio=src.name, start=start, duration=duration, key=cache_key)
                return Transcript.load(cached)

        work = ((self.cache_dir or Path(os.environ.get("TMPDIR", "/tmp"))) / "whisper-work" / uuid.uuid4().hex).resolve()
        work.mkdir(parents=True, exist_ok=True)
        try:
            seek = f"-ss {start:.3f} " if start > 0 else ""
            span = f"-t {duration:.3f} " if duration is not None else ""
            script = (
                "set -e\n"
                f"{cfg.ffmpeg} -nostdin -loglevel error {seek}{span}-i /in/{shlex.quote(src.name)} "
                "-ar 16000 -ac 1 -c:a pcm_s16le -f wav /out/audio.wav\n"
                f"{cfg.whisper_cli} -m /models/{shlex.quote(Path(cfg.model_path).name)} -f /out/audio.wav "
                f"-l {shlex.quote(lang or 'auto')} -t {cfg.threads} -bo {cfg.best_of} -bs {cfg.beam_size} "
                f"{' '.join(shlex.quote(a) for a in cfg.extra_args)} -oj -ojf -of /out/result >/out/whisper.log 2>&1\n"
            )
            mounts = {
                src.parent: ("/in", True),
                Path(cfg.model_path).parent: ("/models", True),
                work: ("/out", False),
            }
            t0 = time.monotonic()
            logger.info("transcription_started", audio=src.name, start=start, duration=duration,
                        language=lang or "auto", engine=cfg.label)
            self.calls += 1
            self._run(script, mounts)
            elapsed = time.monotonic() - t0
            result_path = work / "result.json"
            if not result_path.exists():
                log = (work / "whisper.log").read_text(errors="replace") if (work / "whisper.log").exists() else ""
                raise TranscriptionError(f"whisper-cli produced no output for {src.name}: {log[-2000:]}")
            data = json.loads(result_path.read_text(encoding="utf-8"))
            transcript = parse_whisper_json(data, offset_seconds=start)
            audio_seconds = duration if duration is not None else (transcript.end - start)
            transcript.engine = cfg.label
            transcript.source = str(src)
            transcript.duration = None if duration is None else start + duration
            transcript.meta.update(
                {
                    "audio_sha256": file_sha256(src),
                    "window": [start, None if duration is None else start + duration],
                    "language_requested": lang or "auto",
                    "elapsed_seconds": round(elapsed, 1),
                    "realtime_factor": round(audio_seconds / elapsed, 3) if elapsed > 0 and audio_seconds else None,
                    "config": cfg.cache_fields(),
                }
            )
            logger.info("transcription_finished", audio=src.name, cues=len(transcript.cues),
                        elapsed=round(elapsed, 1), realtime_factor=transcript.meta["realtime_factor"],
                        language=transcript.language)
            if cache_key is not None:
                transcript.save(self.cache_dir / "whisper" / f"{cache_key}.json")  # type: ignore[operator]
            return transcript
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- plumbing -------------------------------------------------------------------------

    def _run(self, script: str, mounts: Dict[Path, Tuple[str, bool]]) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(script, mounts)
        cfg = self.config
        for host in mounts:
            if not host.is_absolute():
                raise TranscriptionError(f"mount path must be absolute: {host}")
        if cfg.docker_image:
            cmd: List[str] = [
                cfg.docker, "run", "--rm", f"--memory={cfg.memory_limit}", "--network=none",
                "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "bash",
            ]
            for host, (inside, ro) in mounts.items():
                cmd += ["-v", f"{host}:{inside}{':ro' if ro else ''}"]
            cmd += [cfg.docker_image, "-c", script]
        else:
            # Run natively: rewrite the container paths to host paths.
            for host, (inside, _ro) in mounts.items():
                script = script.replace(inside + "/", str(host) + "/")
            cmd = ["bash", "-c", script]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(f"transcription timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise TranscriptionError(
                f"command failed ({proc.returncode}): {' '.join(cmd[:6])}...\n"
                f"{proc.stderr.strip()[-2000:]}"
            )
        return proc
