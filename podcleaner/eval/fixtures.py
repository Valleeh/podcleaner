"""Pinned real-episode fixtures for the integration tests.

``tests/integration/manifest.json`` names every episode, every audio *variant* of it
(``clean`` = plain HTTP fetch, ``podcatcher`` = fetched with a podcatcher User-Agent,
which is what listeners actually receive) and every transcript, each pinned by SHA-256.
The bytes themselves live under ``var/fixtures/`` (gitignored: they are commercial
podcasts) and are downloaded on request.

Why the pinning is strict: all three shows insert advertising server-side, so two
downloads of "the same episode" differ, and labels made for one file are meaningless
against another.  A hash mismatch is therefore a *skip with an explanation*, never a
silent pass and never a comparison against the wrong bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from podcleaner.logging import get_logger

__all__ = ["Episode", "Fixture", "FixtureError", "FixtureStore", "REPO_ROOT", "load_manifest", "sha256_of"]

logger = get_logger(__name__)

PathLike = Union[str, Path]
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "integration" / "manifest.json"
DEFAULT_ROOT = REPO_ROOT / "var" / "fixtures"


class FixtureError(RuntimeError):
    """A fixture is missing, could not be fetched, or does not match its pin."""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


def sha256_of(path: PathLike) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class Fixture:
    """One pinned file: where it lives under the store, how to fetch it, what it must hash to."""

    kind: str            # directory under the store: "audio" | "transcripts"
    file: str            # relative to var/fixtures/<kind>/
    sha256: Optional[str]
    url: Optional[str] = None
    user_agent: Optional[str] = None
    duration: Optional[float] = None
    meta: dict = field(default_factory=dict)


@dataclass
class Episode:
    id: str
    podcast: str
    title: str
    language: str
    guid: Optional[str]
    feed_url: Optional[str]
    enclosure_url: Optional[str]
    audio: Dict[str, Fixture]          # variant -> fixture
    transcripts: Dict[str, Fixture]    # name -> fixture (e.g. "official", "whisper-small")
    label: Optional[str]               # path relative to tests/integration
    windows: List[dict]
    dai: Optional[dict] = None         # {"clean": variant, "stitched": variant, "file": ...}
    notes: str = ""


def load_manifest(path: PathLike = DEFAULT_MANIFEST) -> Dict[str, Episode]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    episodes: Dict[str, Episode] = {}
    for eid, e in raw["episodes"].items():
        audio = {
            variant: Fixture("audio", a["file"], a.get("sha256"), a.get("url"), a.get("user_agent"), a.get("duration"),
                             {k: v for k, v in a.items() if k not in {"file", "sha256", "url", "user_agent", "duration"}})
            for variant, a in e.get("audio", {}).items()
        }
        transcripts = {
            name: Fixture("transcripts", t["file"], t.get("sha256"), t.get("url"), None, None,
                          {k: v for k, v in t.items() if k not in {"file", "sha256", "url"}})
            for name, t in e.get("transcripts", {}).items()
        }
        episodes[eid] = Episode(
            id=eid, podcast=e["podcast"], title=e.get("title", eid), language=e["language"],
            guid=e.get("guid"), feed_url=e.get("feed_url"), enclosure_url=e.get("enclosure_url"),
            audio=audio, transcripts=transcripts, label=e.get("label"), windows=e.get("windows", []),
            dai=e.get("dai"), notes=e.get("notes", ""),
        )
    return episodes


class FixtureStore:
    """Resolves pinned fixtures to local files, downloading when allowed."""

    def __init__(self, root: PathLike = DEFAULT_ROOT, *, allow_download: Optional[bool] = None) -> None:
        self.root = Path(root)
        if allow_download is None:
            allow_download = os.environ.get("PODCLEANER_IT_DOWNLOAD", "") not in ("", "0", "false", "no")
        self.allow_download = allow_download

    def path(self, fixture: Fixture) -> Path:
        return self.root / fixture.kind / fixture.file

    def resolve(self, fixture: Fixture, *, verify: bool = True) -> Path:
        """Local path of ``fixture``; fetch it if missing and allowed; check the hash."""
        dest = self.path(fixture)
        if not dest.exists():
            if not fixture.url:
                raise FixtureError(
                    f"{dest} is missing and has no URL; it must be obtained out of band "
                    f"(see tests/integration/README.md)", kind="missing")
            if not self.allow_download:
                raise FixtureError(
                    f"{dest} is missing; re-run with --download (or PODCLEANER_IT_DOWNLOAD=1) "
                    f"to fetch {fixture.url}", kind="missing")
            self._download(fixture, dest)
        if verify and fixture.sha256:
            actual = sha256_of(dest)
            if actual != fixture.sha256:
                raise FixtureError(
                    f"{dest} has sha256 {actual[:12]}..., manifest pins {fixture.sha256[:12]}...; "
                    f"a fresh download of a dynamically ad-inserted episode never matches the "
                    f"labelled bytes -- re-pin the manifest and relabel, or restore the original file",
                    kind="hash_mismatch")
        return dest

    def _download(self, fixture: Fixture, dest: Path) -> None:
        import requests

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {"User-Agent": fixture.user_agent} if fixture.user_agent else {}
        logger.info("fixture_download", url=fixture.url, dest=str(dest), user_agent=fixture.user_agent)
        with requests.get(fixture.url, headers=headers, stream=True, timeout=120, allow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                shutil.copyfileobj(r.raw, fh)
        os.replace(tmp, dest)

    def audio(self, episode: Episode, variant: str = "podcatcher") -> Path:
        if variant not in episode.audio:
            raise FixtureError(f"{episode.id} has no {variant!r} audio variant", kind="missing")
        return self.resolve(episode.audio[variant])

    def transcript(self, episode: Episode, name: str) -> Path:
        if name not in episode.transcripts:
            raise FixtureError(f"{episode.id} has no {name!r} transcript", kind="missing")
        return self.resolve(episode.transcripts[name])
