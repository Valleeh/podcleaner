"""The acceptance measure: how much advertising came out, and did any programme go too.

    ./run verify <feed-url> <guid>

Reads what the pipeline published for that episode, then checks it against the publisher
itself. Podcast hosts that stitch advertising in serve the ad-free master to a plain
User-Agent and the stitched variant to a podcatcher, and the stitch reuses the master's
MP3 frames byte for byte -- so walking the two frame sequences recovers the inserted
regions exactly, to one frame (26 ms). That is ground truth: it shares no code with the
cutter, reads no container header, and needs no hand labels.

The check is only valid if the bytes we re-fetch are the bytes we cut, so the re-fetched
stitch must match the sha256 the verdict recorded. If the publisher has re-stitched the
episode since, this says so and stops rather than reporting a number that means nothing.

Not part of the product: the server does not import this, and the plain-UA fetch here is
a measurement, not a detection tier.  It shares no code with the server either -- it finds
the episode by recomputing the store key below rather than by asking, so a server that
put the files somewhere else is caught here instead of quietly verifying nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.dai import find_inserted_regions  # noqa: E402

#: The UA a real podcatcher sends -- the variant every listener gets, ads included, and
#: therefore the only variant this project is entitled to reason about.  It must be the one
#: the server fetched with, or the re-fetch below is not a re-fetch of the bytes that were
#: cut: a stitching CDN chooses what to serve by this header.  Keep it in step by hand;
#: the two are deliberately separate programs (see the module docstring).
PODCATCHER_USER_AGENT = "AntennaPod/3.6.0"

#: A User-Agent the stitching CDNs treat as "not a podcatcher" and answer with the master.
PLAIN_USER_AGENT = "curl/8.5.0"

TIMEOUT_SECONDS = 900


def episode_dir(feed_url: str, guid: str) -> Path:
    """The directory the server keeps this episode in: sha256 of the feed and the guid.

    Keyed on both because a guid is only unique within its own feed, and several shows
    are served through one server.  Recomputed here rather than imported, so the server
    and its acceptance measure agree by both being right rather than by sharing a line.
    """
    root = Path(os.environ.get("PODCLEANER_STORE_ROOT") or Path(__file__).resolve().parents[1] / "var/episodes")
    key = hashlib.sha256(feed_url.strip().encode() + b"\0" + guid.strip().encode()).hexdigest()
    return root / key[:32]


def _download(url: str, dest: Path, user_agent: str) -> str:
    """Stream ``url`` to ``dest`` under ``user_agent``; return the sha256 of what landed."""
    h = hashlib.sha256()
    with requests.get(url, headers={"User-Agent": user_agent}, stream=True,
                      timeout=TIMEOUT_SECONDS) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for block in resp.iter_content(1 << 16):
                fh.write(block)
                h.update(block)
    return h.hexdigest()


def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def verify(feed_url: str, guid: str) -> int:
    d = episode_dir(feed_url, guid)
    verdict_path, source_path = d / "verdict.json", d / "source.json"
    if not verdict_path.exists():
        print(f"no verdict at {verdict_path} -- nothing has been published for this episode")
        return 2
    verdict = json.loads(verdict_path.read_text())
    origin = json.loads(source_path.read_text())["url"]
    removed = [tuple(r) for r in verdict.get("removed", [])]
    print(f"episode   {d.name}   state={verdict['state']}   "
          f"removed={verdict.get('removed_seconds', 0)} s in {len(removed)} interval(s)")

    work = Path(tempfile.mkdtemp(prefix="podcleaner-verify-"))
    try:
        stitch, master = work / "stitch.mp3", work / "master.mp3"
        got = _download(origin, stitch, PODCATCHER_USER_AGENT)
        want = (verdict.get("source_sha256") or "").removeprefix("sha256:")
        if want and got != want:
            print(f"the publisher has re-stitched this episode since it was cut\n"
                  f"  cut from  sha256:{want}\n  now serves sha256:{got}\n"
                  f"cannot verify a cut against bytes it was not made from")
            return 3
        _download(origin, master, PLAIN_USER_AGENT)

        walk = find_inserted_regions(master, stitch)
        # No inserted regions means the two variants are the same file, so this measure has
        # nothing to measure: it recovers advertising by diffing them. Saying so is not a
        # detail. The containment arithmetic below divides the removed intervals among the
        # true ads, and with no true ads every interval trivially falls outside one -- so a
        # correct cut of advertising the publisher baked in reports CONTAINMENT FAILED and
        # names the whole cut as programme lost. That is the most alarming line this tool
        # can print, and it would be false.
        if not walk.regions:
            print(f"\nthe publisher serves the same bytes to both user agents: no advertising is\n"
                  f"inserted into this episode, so there is nothing here to compare a cut against.\n"
                  f"NOT MEASURABLE      this episode carries no dynamically inserted advertising.\n"
                  f"                    Whatever was removed was in the publisher's own master, and\n"
                  f"                    whether it was advertising is not a question this can answer.")
            return 4
        if walk.skipped_clean_seconds > 0.5:
            print(f"warning: the frame walk skipped {walk.skipped_clean_seconds:.3f} s of the "
                  f"master at splices; treat the margins below as approximate")
        print(f"\nadvertising in the file the listener would have got: "
              f"{walk.total_inserted:.3f} s in {len(walk.regions)} break(s)")
        for i, g in enumerate(walk.regions, 1):
            mine = [c for c in removed if c[0] < g.end and c[1] > g.start]
            took = sum(min(c[1], g.end) - max(c[0], g.start) for c in mine)
            print(f"  {i}  {_clock(g.start)} - {_clock(g.end)}  ({g.duration:7.3f} s)   "
                  f"removed {took:7.3f} s")

        contained, escaped = True, []
        for s, e in removed:
            if not any(g.start <= s and e <= g.end for g in walk.regions):
                contained = False
                escaped.append((s, e))
        ad_removed = sum(min(e, g.end) - max(s, g.start)
                         for s, e in removed for g in walk.regions
                         if not (e <= g.start or s >= g.end))
        stray = sum(e - s for s, e in removed) - ad_removed

        print()
        if contained:
            print("PROGRAMME INTACT   every removed interval lies inside a true ad")
        else:
            print(f"CONTAINMENT FAILED  {len(escaped)} interval(s) reach outside a true ad, "
                  f"{stray:.3f} s of programme removed:")
            for s, e in escaped:
                print(f"    {_clock(s)} - {_clock(e)}")
        pct = 100 * ad_removed / walk.total_inserted if walk.total_inserted else 0.0
        print(f"ADVERTISING        {ad_removed:.3f} s of {walk.total_inserted:.3f} s removed "
              f"({pct:.1f}%), {walk.total_inserted - ad_removed:.3f} s still plays")
        return 0 if contained else 1
    finally:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        work.rmdir()


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    return verify(argv[0].strip(), argv[1].strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
