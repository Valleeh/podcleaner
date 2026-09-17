"""Is the deployed thing actually working?

    ./run health [feed-url]

Every outage this project has had was one of these five, and not one of them was a bad
cut: the server had died with its session, it was running code from before the last
merge, the public route existed only in Caddy's memory and vanished on a restart, no feed
had ever been subscribed through it, and a bug that made every episode FAIL shipped past
a green suite. So this checks the deployment, not the algorithm.

Exit code is the number of failing checks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
LOCAL = f"http://127.0.0.1:{os.environ.get('PODCLEANER_PORT', '8080')}"
PUBLIC = os.environ.get("PODCLEANER_BASE_URL", LOCAL)

OK, BAD = "ok  ", "FAIL"


def _say(good: bool, name: str, detail: str) -> bool:
    print(f"{OK if good else BAD}  {name:22s} {detail}")
    return good


def _server_pid() -> int | None:
    out = subprocess.run(["pgrep", "-f", "^docker run .* podclean$"], capture_output=True, text=True)
    for line in out.stdout.split():
        pid = int(line)
        if pid != os.getpid():
            return pid
    return None


def check_process() -> bool:
    pid = _server_pid()
    if pid is None:
        return _say(False, "server process", "no `./run serve` (docker run ... podclean) is running")
    return _say(True, "server process", f"pid {pid}")


def _image_built_at() -> float | None:
    """When the image the server runs was built, as a unix timestamp.

    Docker prints RFC 3339 with nanoseconds, which ``fromisoformat`` cannot read, and with
    whatever zone the daemon feels like -- a ``Z`` or a ``+02:00``. Dropping the zone and
    calling the rest UTC is how this check first reported a freshly built image as two
    hours stale.
    """
    out = subprocess.run(["docker", "image", "inspect", "-f", "{{.Created}}", "podclean"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$",
                 out.stdout.strip())
    if m is None:
        return None
    zone = m.group(3) or "+00:00"
    return datetime.fromisoformat(
        f"{m.group(1)}.{(m.group(2) or '0')[:6]}{'+00:00' if zone == 'Z' else zone}").timestamp()


def check_code_is_current() -> bool:
    """A compiled server can be stale in two ways, and they need different fixes.

    The source can be newer than the image, which means `./run build`; or the image can be
    newer than the running process, which means a restart. Both have happened, and being
    told the wrong one of the two costs a confused ten minutes.

    Start time comes from the mtime of /proc/<pid>, not from ``ps -o etimes``, which
    reports 130 years on this host.
    """
    pid = _server_pid()
    if pid is None:
        return _say(False, "code is current", "no process to compare against")
    built = _image_built_at()
    if built is None:
        return _say(False, "code is current", "no `podclean` image -- ./run build")
    newest, name = max((p.stat().st_mtime, str(p.relative_to(ROOT)))
                       for p in (ROOT / "podclean").glob("**/*.go"))
    if newest > built:
        return _say(False, "code is current",
                    f"{name} changed {(newest - built) / 60:.0f} min after the image was "
                    f"built -- the binary is the old code, ./run build")
    started = Path(f"/proc/{pid}").stat().st_mtime
    if built > started:
        return _say(False, "code is current",
                    f"the image was rebuilt {(built - started) / 60:.0f} min after the "
                    f"server started -- it is running the old binary, restart it")
    return _say(True, "code is current", "the image is built from the checkout and is what is running")


def check_local() -> bool:
    try:
        r = requests.get(f"{LOCAL}/podcast", timeout=10)
    except requests.RequestException as exc:
        return _say(False, "app answers", f"{LOCAL} unreachable: {exc}")
    return _say(r.status_code == 400, "app answers",
                f"{LOCAL}/podcast -> {r.status_code} (400 = alive and validating)")


def check_public_route() -> bool:
    """Ask the running server what URL it hands out, not this shell -- the enclosure URLs
    a podcatcher follows are built from the server's own PODCLEANER_BASE_URL."""
    public = PUBLIC
    pid = _server_pid()
    if pid is not None:
        try:
            env = Path(f"/proc/{pid}/environ").read_text().split("\0")
            public = next((v.split("=", 1)[1] for v in env
                           if v.startswith("PODCLEANER_BASE_URL=")), PUBLIC)
        except OSError:
            pass
    if public.startswith("http://127.0.0.1") or public.startswith("http://localhost"):
        return _say(True, "public route", "not configured (server hands out a local base url)")
    try:
        r = requests.get(f"{public}/rss", timeout=20)
    except requests.RequestException as exc:
        return _say(False, "public route", f"{public} unreachable: {exc}")
    good = r.status_code in (400, 401)
    return _say(good, "public route",
                f"{public}/rss -> {r.status_code}" + ("" if good else " (expected 401 or 400)"))


def check_feed(feed_url: str | None) -> bool:
    if not feed_url:
        return _say(True, "feed rewrite", "no feed given, skipped")
    try:
        r = requests.get(f"{LOCAL}/rss?feed={quote(feed_url, safe='')}", timeout=120)
    except requests.RequestException as exc:
        return _say(False, "feed rewrite", f"{exc}")
    n = r.text.count("/podcast?")
    return _say(r.status_code == 200 and n > 0, "feed rewrite",
                f"{r.status_code}, {n} enclosure(s) pointing back here")


def check_last_verdict() -> bool:
    root = Path(os.environ.get("PODCLEANER_STORE_ROOT", ROOT / "var" / "episodes"))
    verdicts = sorted(root.glob("*/verdict.json"), key=lambda p: p.stat().st_mtime)
    if not verdicts:
        return _say(False, "last episode", "no episode has ever been through the pipeline")
    v = json.loads(verdicts[-1].read_text())
    # untouched is a decision like any other -- an episode too long to examine, served
    # whole and unbilled. Only `failed` and a state this tool does not know are outages.
    good = v.get("state") in ("cut", "clean", "refused", "untouched")
    return _say(good, "last episode",
                f"{verdicts[-1].parent.name} state={v.get('state')} "
                f"removed={v.get('removed_seconds', 0)} s")


def main(argv) -> int:
    feed = argv[0].strip() if argv else None
    checks = [check_process(), check_code_is_current(), check_local(),
              check_public_route(), check_feed(feed), check_last_verdict()]
    failed = sum(1 for c in checks if not c)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
