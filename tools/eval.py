"""The model against episodes whose advertising is known: did programme go, did ads stay.

    ./run eval [case ...] [--runs N]     measure every case (or the ones named), N runs each
    ./run eval --add <case> <feed> <guid>
                                         make a case of an episode the live server played
    ./run eval --transcript <case>       print a case's transcript, to write its labels from

A change to the prompt or the model is measured here, across every case, before it is
deployed -- not on the episode that prompted it.  Tuned on the last bad episode alone, each
change quietly breaks the one before: the switch to one model pass was measured on SOLVED
and cut three minutes of Hacks On Tap's own discussion of campaign ads.

``./run test`` cannot answer this.  Its model is a stub that says what the test tells it
to; what the real model makes of a real transcript is only seen by asking it, which costs
money and comes out differently from run to run.  So this is host-only, like
``./run verify``, and runs each case several times.

Each case is a directory under ``var/eval/cases/`` -- commercial content, never committed:

    heard.json   the transcript, as the live server keeps it for every episode it plays
    case.json    {"title", "language", "ads": [{"start", "end", "what", "optional"?}]}
    silence.mp3  made here, once: silence as long as the episode
    runs/        every run's verdict.json and heard.json, one directory per eval: what the
                 model was shown and what it proposed

``ads`` are in seconds of the episode.  Everything outside them is programme.  An
``optional`` ad may be cut or left -- the show's own spots and tours, which the owner
leaves in, or a hand-off into a break.

No audio is kept and nothing is heard again.  Nothing between the download and the cut
listens to the audio -- the model reads the transcript, a cut is placed on its words --
so the real server runs, as ``./run test`` runs it, on silence as long as the episode,
against a stand-in transcriber that answers with ``heard.json``.  The model is the real
one, at the server's own setting, and only its reading differs between runs.  Any
episode the live server has played is a case the moment it is labelled.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.integration.support import Outside, ffmpeg, podclean_server  # noqa: E402
from tools.health import _image_label, source_digest  # noqa: E402
from tools.verify import episode_dir  # noqa: E402

CASES = REPO / "var/eval/cases"
OPENROUTER = "https://openrouter.ai/api/v1"

#: Advertising that may still play at each end of a break: the owner's bar of 2026-10-09.
EDGE_SECONDS = 3.0

#: Advertising that may still play between two back-to-back spots cut separately: same bar.
BETWEEN_SPOTS_SECONDS = 5.0

#: More advertising than this left in one place is a spot the model never reported, not an
#: edge: the shortest spot in the cases is about fifteen seconds.
SPOT_SECONDS = 10.0

#: Programme removed up to this is frame rounding at a cut edge, not a cut into the talk.
PROGRAMME_TOLERANCE_SECONDS = 0.5


@dataclass(frozen=True)
class Ad:
    start: float
    end: float
    what: str
    optional: bool = False


@dataclass(frozen=True)
class Case:
    name: str
    dir: Path
    title: str
    language: str
    ads: tuple[Ad, ...]

    @property
    def heard(self) -> dict:
        return json.loads((self.dir / "heard.json").read_text())


def load(name: str) -> Case:
    d = CASES / name
    spec = json.loads((d / "case.json").read_text())
    return Case(name, d, spec["title"], spec["language"],
                tuple(Ad(a["start"], a["end"], a["what"], a.get("optional", False))
                      for a in spec["ads"]))


def _key() -> str:
    return json.loads((REPO / ".secret.json").read_text())["openrouter-token"]


def _clock(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def silence(case: Case) -> Path:
    """Silence as long as the episode, made once: low bitrate, because nobody listens."""
    path = case.dir / "silence.mp3"
    if not path.exists():
        ffmpeg(["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=22050",
                "-t", f"{case.heard['duration_seconds']:.3f}", "-c:a", "libmp3lame",
                "-b:a", "16k", "-y"], writes=path)
    return path


def transcriber(case: Case):
    """The transcription endpoint, answering the episode with heard.json.

    The server is run so that it sends the episode up in one request; that one is answered
    with the whole transcript.  What it asks after that are the stretches with no words in
    them -- and heard.json already holds whatever a second model heard there -- so those are
    answered with nothing.
    """
    heard = case.heard
    whole = json.dumps({
        "task": "transcribe", "duration": heard["duration_seconds"],
        "text": " ".join(c["text"] for c in heard["cues"]),
        "segments": [{"id": i, "start": c["start"], "end": c["end"], "text": f" {c['text']}"}
                     for i, c in enumerate(heard["cues"])],
        "words": [{"word": f" {w['text']}", "start": w["start"], "end": w["end"]}
                  for c in heard["cues"] for w in c["words"]],
    }).encode("utf-8")
    nothing = json.dumps({"text": "", "segments": [], "words": []}).encode("utf-8")
    asked = []

    def answer(_posted: bytes) -> bytes:
        asked.append(1)
        return whole if len(asked) == 1 else nothing

    return answer


def run_once(case: Case, key: str, keep: Path) -> dict:
    """One listener's first play of the case, through the real server; its verdict.

    What the server wrote -- verdict.json, and heard.json, the transcript the model was
    asked about -- is kept as ``keep`` with those suffixes.
    """
    outside = Outside()
    audio = silence(case).read_bytes()
    outside.serves("/audio.mp3", "audio/mpeg", audio)
    outside.serves("/feed.xml", "application/rss+xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>{case.title}</title><language>{case.language}</language>
<item><title>{case.title}</title><guid>eval</guid>
<enclosure url="{outside.url}/audio.mp3" type="audio/mpeg" length="{len(audio)}"/></item>
</channel></rss>""")
    outside.serves("/audio/transcriptions", "application/json", transcriber(case))
    try:
        with tempfile.TemporaryDirectory(prefix="podclean-eval-") as tmp, \
                podclean_server(outside, Path(tmp), PODCLEANER_LLM_BASE_URL=OPENROUTER,
                                PODCLEANER_LLM_SPEC=os.environ.get("PODCLEANER_LLM_SPEC", ""),
                                PODCLEANER_LLM_API_KEY=key,
                                PODCLEANER_TRANSCRIBE_MAX_BYTES=str(1 << 30)) as url:
            feed = f"{outside.url}/feed.xml"
            requests.get(f"{url}/rss", params={"feed": feed}, timeout=120).raise_for_status()
            played = requests.get(f"{url}/podcast", params={"feed": feed, "guid": "eval"}, timeout=3600)
            if played.status_code != 200:
                return {"state": "failed", "error": f"/podcast answered {played.status_code}: {played.text[:200]}"}
            for _ in range(360):
                found = list(Path(tmp).glob("episodes/*/verdict.json"))
                if found:
                    for written in found[0].parent.glob("*.json"):
                        if written.name in ("verdict.json", "heard.json"):
                            keep.with_suffix(f".{written.name}").write_bytes(written.read_bytes())
                    return json.loads(found[0].read_text())
                time.sleep(5)
            return {"state": "failed", "error": "no verdict after 30 minutes"}
    finally:
        outside.httpd.shutdown()


def _overlap(a: float, b: float, s: float, e: float) -> float:
    return max(0.0, min(b, e) - max(a, s))


def score(case: Case, verdict: dict) -> tuple[set[str], list[str]]:
    """How the run fell short of the bar (nothing: it passes), and the lines that show it.

    Three ways, kept apart because they have different causes: programme cut (the model
    took talk for advertising), ad missed (it never reported a break), edges (it found the
    break, but the cut leaves more of it playing than the bar allows).
    """
    removed = [tuple(r) for r in verdict.get("removed", [])]
    lines, faults = [], set()
    if verdict["state"] != "cut":
        faults.add(verdict["state"])
        lines.append(f"{verdict['state']}: {verdict.get('error', '')[:200]}")

    programme = []
    for s, e in removed:
        pieces = [(s, e)]
        for ad in case.ads:
            pieces = [p for a, b in pieces
                      for p in ((a, min(b, ad.start)), (max(a, ad.end), b)) if p[1] - p[0] > 0.05]
        programme += pieces
    if sum(b - a for a, b in programme) > PROGRAMME_TOLERANCE_SECONDS:
        faults.add("programme cut")
    for a, b in programme:
        lines.append(f"programme cut  {_clock(a)}-{_clock(b)}  ({b - a:.1f} s)")

    for ad in (a for a in case.ads if not a.optional):
        cuts = sorted((max(s, ad.start), min(e, ad.end)) for s, e in removed if s < ad.end and e > ad.start)
        where = f"{_clock(ad.start)}-{_clock(ad.end)}"
        if not cuts:
            faults.add("ad missed")
            lines.append(f"ad missed      {where}  {ad.end - ad.start:.1f} s  {ad.what}")
            continue
        head, tail = cuts[0][0] - ad.start, ad.end - cuts[-1][1]
        gaps = [b[0] - a[1] for a, b in zip(cuts, cuts[1:])]
        if max([head, tail, *gaps]) > SPOT_SECONDS:
            faults.add("ad missed")
        if head > EDGE_SECONDS or tail > EDGE_SECONDS or any(g > BETWEEN_SPOTS_SECONDS for g in gaps):
            faults.add("edges")
            inside = f", {sum(gaps):.1f} s between cuts" if gaps else ""
            lines.append(f"ad partly in   {where}  {head:.1f} s at the start, {tail:.1f} s at the "
                         f"end{inside}  {ad.what}")
    return faults, lines


def _usage(key: str) -> float | None:
    try:
        r = requests.get(f"{OPENROUTER}/credits", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        return r.json()["data"]["total_usage"]
    except Exception:
        return None


def show_transcript(case: Case) -> None:
    """The case's transcript on the episode's own clock, one cue per line."""
    for c in case.heard["cues"]:
        print(f"[{c['cue']}] {_clock(c['start']):>8} {_clock(c['end']):>8}  {c['text']}")


def add(name: str, feed: str, guid: str) -> int:
    """A new case from an episode the live server played: its heard.json, no labels yet."""
    played = episode_dir(feed, guid)
    if not (played / "heard.json").exists():
        print(f"no heard.json in {played}: the live server has not played this episode since it"
              f" began keeping one")
        return 2
    d = CASES / name
    d.mkdir(parents=True, exist_ok=False)
    shutil.copy(played / "heard.json", d / "heard.json")
    language = json.loads((played / "source.json").read_text()).get("language", "")
    (d / "case.json").write_text(json.dumps(
        {"title": name, "language": language, "notes": f"{feed} {guid}", "ads": []}, indent=1))
    print(f"{d}: label its ads in case.json, from ./run eval --transcript {name}")
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="./run eval")
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--transcript", metavar="CASE")
    ap.add_argument("--add", nargs=3, metavar=("CASE", "FEED", "GUID"))
    args = ap.parse_args(argv)
    if args.transcript:
        show_transcript(load(args.transcript))
        return 0
    if args.add:
        return add(*args.add)
    if "PODCLEAN_SERVER_CMD" not in os.environ and _image_label() != source_digest():
        print("the podclean image is not built from these sources: ./run build first")
        return 2

    key = _key()
    cases = [load(n) for n in (args.cases or sorted(p.name for p in CASES.iterdir() if p.is_dir()))]
    before, results = _usage(key), {}
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for case in cases:
        runs = case.dir / "runs" / stamp
        runs.mkdir(parents=True, exist_ok=True)
        for run in range(1, args.runs + 1):
            started = time.time()
            verdict = run_once(case, key, runs / str(run))
            (runs / f"{run}.verdict.json").write_text(json.dumps(verdict, indent=1))
            faults, lines = score(case, verdict)
            replayed = runs / f"{run}.heard.json"
            if not replayed.exists():
                lines.append("the server wrote no heard.json")
            elif len(json.loads(replayed.read_text())["cues"]) != len(case.heard["cues"]):
                lines.append("the server heard the replayed transcript differently: cue numbers moved")
            results.setdefault(case.name, []).append(faults)
            print(f"{case.name:22s} run {run}  {'FAIL' if faults else 'PASS'}  {verdict['state']:7s} "
                  f"removed {verdict.get('removed_seconds', 0):6.1f} s  ({time.time() - started:.0f} s)",
                  flush=True)
            for line in lines:
                print(f"    {line}", flush=True)

    print()
    for name, runs_faults in results.items():
        counted = {f: sum(f in fs for fs in runs_faults) for f in sorted(set().union(*runs_faults))}
        why = ", ".join(f"{f} in {n}" for f, n in counted.items())
        print(f"{name:22s} {sum(not fs for fs in runs_faults)}/{len(runs_faults)} runs pass"
              f"{'  -- ' + why if why else ''}")
    after = _usage(key)
    if before is not None and after is not None:
        print(f"\ncost of this eval: about ${after - before:.3f}")
    return 0 if not any(fs for runs_faults in results.values() for fs in runs_faults) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
