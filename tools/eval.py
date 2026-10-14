"""The model against episodes whose advertising is known: did programme go, did ads stay.

    ./run eval [case ...] [--runs N]     measure every case (or the ones named), N runs each
    ./run eval --transcript <case>       print a case's transcript, to write its labels from

A change to the prompt or the model is measured here, across every case, before it is
deployed -- not on the episode that prompted it.  Tuned on the last bad episode alone, each
change quietly breaks the one before: the switch to one model pass was measured on SOLVED
and cut three minutes of Hacks On Tap's own discussion of campaign ads.

``./run test`` cannot answer this.  Its model is a stub that says what the test tells it
to; what the real model makes of a real transcript is only seen by asking it, which costs
money and comes out differently from run to run.  So this is host-only, like
``./run verify``, and runs each case several times.

Each case is a directory under ``var/eval/cases/`` -- commercial audio, never committed:

    audio.mp3    the episode as a podcatcher got it
    case.json    {"title", "language", "ads": [{"start", "end", "what", "optional"?}]}
    replies/     the transcription endpoint's answers, keyed by model and audio
    runs/        every run's verdict.json, one directory per eval, to see what the model proposed

``ads`` are in seconds of ``audio.mp3``.  Everything outside them is programme.  An
``optional`` ad may be cut or left -- a host endorsement woven into the talk, or an ad the
publisher baked into its master that no frame walk can tell from programme.

The real server runs, as ``./run test`` runs it, against a local stand-in for the
publisher (a feed and ``audio.mp3``) and for the transcriber, which answers from
``replies/`` and asks OpenRouter only about audio it has not heard before.  The model is
the real one, at the server's own setting.  So every stage a listener's episode goes
through runs, and only the model's reading differs between runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.integration.episode import _posted_audio, _posted_field  # noqa: E402
from tests.integration.support import Outside, podclean_server  # noqa: E402
from tools.dai import _frames_of  # noqa: E402
from tools.health import _image_label, source_digest  # noqa: E402

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


def transcriber(case: Case, key: str, live: list):
    """The transcription endpoint, answering from ``replies/`` and asking OpenRouter once.

    A reply is keyed by the model asked and the audio sent, so a server that cuts the
    episode into different pieces, or asks about different holes, is answered by the real
    endpoint rather than by a reply to some other question.  Where each piece starts in
    the episode is written to ``replies/index.json`` for ``--transcript``.
    """
    replies = case.dir / "replies"
    replies.mkdir(exist_ok=True)

    def answer(posted: bytes) -> bytes:
        model, audio = _posted_field(posted, "model"), _posted_audio(posted)
        name = hashlib.sha256(model.encode() + b"\0" + audio).hexdigest()[:32]
        saved = replies / f"{name}.json"
        if not saved.exists():
            boundary = posted.split(b"\r\n", 1)[0][2:].decode()
            reply = requests.post(f"{OPENROUTER}/audio/transcriptions", data=posted, timeout=900,
                                  headers={"Authorization": f"Bearer {key}",
                                           "Content-Type": f"multipart/form-data; boundary={boundary}"})
            reply.raise_for_status()
            saved.write_bytes(reply.content)
            index_path = replies / "index.json"
            index = json.loads(index_path.read_text()) if index_path.exists() else {}
            index[name] = {"model": model, "start": _seconds_into(case, audio)}
            index_path.write_text(json.dumps(index, indent=1))
            live.append(name)
        return saved.read_bytes()

    return answer


def _seconds_into(case: Case, piece: bytes) -> float:
    """Where in ``audio.mp3`` a piece the server sent begins: its first frame, found."""
    whole = (case.dir / "audio.mp3").read_bytes()
    at = whole.find(piece[:4096])
    return sum(f.duration for f in _frames_of(whole) if f.offset < at)


def run_once(case: Case, key: str, live: list) -> dict:
    """One listener's first play of the case, through the real server; its verdict."""
    outside = Outside()
    audio = (case.dir / "audio.mp3").read_bytes()
    outside.serves("/audio.mp3", "audio/mpeg", audio)
    outside.serves("/feed.xml", "application/rss+xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>{case.title}</title><language>{case.language}</language>
<item><title>{case.title}</title><guid>eval</guid>
<enclosure url="{outside.url}/audio.mp3" type="audio/mpeg" length="{len(audio)}"/></item>
</channel></rss>""")
    outside.serves("/audio/transcriptions", "application/json", transcriber(case, key, live))
    try:
        with tempfile.TemporaryDirectory(prefix="podclean-eval-") as tmp, \
                podclean_server(outside, Path(tmp), PODCLEANER_LLM_BASE_URL=OPENROUTER,
                                PODCLEANER_LLM_SPEC=os.environ.get("PODCLEANER_LLM_SPEC", ""),
                                PODCLEANER_LLM_API_KEY=key) as url:
            feed = f"{outside.url}/feed.xml"
            requests.get(f"{url}/rss", params={"feed": feed}, timeout=120).raise_for_status()
            played = requests.get(f"{url}/podcast", params={"feed": feed, "guid": "eval"}, timeout=3600)
            if played.status_code != 200:
                return {"state": "failed", "error": f"/podcast answered {played.status_code}: {played.text[:200]}"}
            for _ in range(360):
                found = list(Path(tmp).glob("episodes/*/verdict.json"))
                if found:
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
    """The case's recorded transcript on the episode's own clock, one segment per line."""
    replies = case.dir / "replies"
    index = json.loads((replies / "index.json").read_text())
    lines = []
    for name, where in index.items():
        for seg in json.loads((replies / f"{name}.json").read_text()).get("segments", []):
            lines.append((where["start"] + seg["start"], where["start"] + seg["end"],
                          "*" if "grok" in where["model"] else " ", seg["text"].strip()))
    for start, end, hole, text in sorted(lines):
        print(f"{_clock(start):>8} {_clock(end):>8} {hole} {text}")


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="./run eval")
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--transcript", metavar="CASE")
    args = ap.parse_args(argv)
    if args.transcript:
        show_transcript(load(args.transcript))
        return 0
    if "PODCLEAN_SERVER_CMD" not in os.environ and _image_label() != source_digest():
        print("the podclean image is not built from these sources: ./run build first")
        return 2

    key = _key()
    cases = [load(n) for n in (args.cases or sorted(p.name for p in CASES.iterdir() if p.is_dir()))]
    before, live, results = _usage(key), [], {}
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for case in cases:
        runs = case.dir / "runs" / stamp
        runs.mkdir(parents=True, exist_ok=True)
        for run in range(1, args.runs + 1):
            started = time.time()
            verdict = run_once(case, key, live)
            (runs / f"{run}.json").write_text(json.dumps(verdict, indent=1))
            faults, lines = score(case, verdict)
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
        print(f"\ncost of this eval: about ${after - before:.3f} "
              f"({len(live)} transcription request(s) went live)")
    return 0 if not any(fs for runs_faults in results.values() for fs in runs_faults) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
