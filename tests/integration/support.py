"""The faked outside world, and the real server pointed at it.

Every test here starts the real server as a real process and speaks to it over HTTP.
Everything the server would reach *out* to is one local stub that answers from canned
data and counts hits, so a test says what the outside world serves and then what the
listener gets, and nothing in between is mocked.

**Nothing here imports the implementation.**  These tests know the server only as a
process that listens on a port.  Rewrite the server in another language, point
``PODCLEAN_SERVER_CMD`` at it, and this suite is the part of the specification it can
check.  It is not the whole of one: the stub answers whatever it is asked and looks at no
file the server writes, so which model, which words are sent to it and what lands on disk
are invisible here and are written down in ``docs/contract.md`` instead.

What this suite requires:

* started as ``$PODCLEAN_SERVER_CMD`` from the repository root, listening on
  ``$PODCLEANER_HOST:$PODCLEANER_PORT`` and calling itself ``$PODCLEANER_BASE_URL`` in the
  links it writes;
* keeping whatever it keeps under ``$PODCLEANER_STORE_ROOT``, in a layout no test looks
  at -- an empty directory is an empty server;
* reaching the audio transcription API under ``$PODCLEANER_TRANSCRIBE_BASE_URL`` and the
  model under ``$PODCLEANER_LLM_BASE_URL``, with ``$PODCLEANER_LLM_SPEC`` and
  ``$PODCLEANER_LLM_API_KEY``, both OpenAI-shaped;
* sending the audio up in requests no larger than
  ``$PODCLEANER_TRANSCRIBE_MAX_BYTES``, because a real transcription endpoint refuses a
  body over its own limit and a full-length episode is several times it;
* answering ``/rss``, ``/podcast``, ``/chapters`` and ``/transcript``.

That is nine environment variables, one command and four routes.  A path the server asks
for that no test described is answered 404 and recorded, so reaching for something this
suite does not know about says so by name rather than hanging: see
:meth:`Outside.unknown_paths`.

The one measurement a test makes for itself, the decoded length of an audio file, is
:func:`decode_seconds` here rather than anything the server exposes, so the ruler and the
thing measured share no code.  The same ffmpeg builds the episode the suite plays, in
:mod:`tests.integration.episode`.  So the audio tests need Docker and nothing else: there
is no downloaded file to have or not have, and nothing here skips.
``PODCLEAN_TEST_FFMPEG_IMAGE`` names the image both of them run in -- the suite's own
knob, not part of the contract above, which says nothing about how a server cuts.

Lives in its own module rather than in a test file because the routes, the feed and the
refusals are separate questions and each wants its own file.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]

#: Override the command to test another implementation through the same HTTP contract.
SERVER_CMD = shlex.split(
    os.environ.get("PODCLEAN_SERVER_CMD", "./run serve"))

#: The image ffmpeg is run from for :func:`decode_seconds`.  There is no ffmpeg on this
#: host's PATH.  Deliberately its own setting rather than the server's, so the ruler keeps
#: working when the server is something else entirely.
FFMPEG_IMAGE = os.environ.get("PODCLEAN_TEST_FFMPEG_IMAGE", "whisper-cpp:local")

_TIME = re.compile(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def ffmpeg(args: list, *, reads: Path | None = None, writes: Path | None = None) -> str:
    """Run ffmpeg in the image, on at most one file in and one file out.

    ``reads`` is mounted read-only at ``/in`` and ``writes`` at ``/out``; a command refers
    to them as ``/in/<name>`` and, for the output, not at all -- it is appended.  The
    container gets no network and a memory limit: this suite must not be the reason the host runs out of memory.
    """
    cmd = ["docker", "run", "--rm", "--memory=256m", "--network=none",
           "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "ffmpeg"]
    if reads is not None:
        cmd += ["-v", f"{reads.resolve().parent}:/in:ro"]
    if writes is not None:
        cmd += ["-v", f"{writes.resolve().parent}:/out"]
    cmd += [FFMPEG_IMAGE, "-hide_banner", "-nostdin", *args]
    if writes is not None:
        cmd += [f"/out/{writes.name}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr.strip()[-2000:]}")
    return proc.stdout + proc.stderr


def decode_seconds(path: Path) -> float:
    """How long ``path`` actually plays for, by decoding all of it.

    A true decode, not a header read.  A container's own duration field disagrees with
    what comes out of the decoder by many seconds on real publisher files, which is more
    than any tolerance worth asserting, so the header is not an oracle for this.

    ffmpeg prints a running ``time=`` while it works and the last one is the answer.  Run
    straight from the image here, sharing nothing with whatever produced the file.
    """
    output = ffmpeg(["-i", f"/in/{path.name}", "-f", "null", "-"], reads=path)
    stamps = _TIME.findall(output)
    if not stamps:
        raise RuntimeError(f"ffmpeg reported no decoded time for {path.name}")
    hours, minutes, seconds = stamps[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _encode(body):
    """A body as bytes: a dict is JSON, a str is UTF-8, a callable is left to be called."""
    if callable(body) or isinstance(body, bytes):
        return body
    return json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")


def model_reply(segments, chapters=()):
    """An editorial decision in the completion endpoint's wire format."""
    return {"choices": [{"message": {"content": json.dumps(
        {"segments": segments, "chapters": list(chapters)})}}]}


class Outside:
    """Everything the server talks to, on one local port, answering from canned data."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.routes: dict[str, tuple[int, str, object]] = {}
        self.limits: dict[str, int] = {}
        self.unknown: list[str] = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def counts(self) -> dict[str, int]:
        """A snapshot of the hit counts, taken under the lock the handlers write behind.

        This is a ``ThreadingHTTPServer``: two handlers can run at once, so ``hits`` is
        read-modify-written from several threads and copying it unguarded can raise.  The
        assertion that nothing outside is asked twice rests on these numbers, so they are
        read the same way they are written.
        """
        with self.lock:
            return dict(self.hits)

    def unknown_paths(self) -> list[str]:
        """Every path asked for that no test described, in the order they were asked.

        Empty is the ordinary case.  Anything in here is a server reaching for something
        this suite does not know about, which is the first thing to look at when a
        reimplementation fails a test for no visible reason.
        """
        with self.lock:
            return list(self.unknown)

    def serves(self, path: str, content_type: str, body, *, status: int = 200,
               refuses_over: int | None = None) -> None:
        """Answer ``path`` with ``body``.

        ``body`` may be a callable, which is handed the request body and returns what to
        answer with.  That is how an endpoint whose reply depends on what was *sent* is
        described -- a transcriber, which must answer about the audio it was given rather
        than about an episode it was told about in advance.

        ``refuses_over`` is a limit on the request body, answered like the real
        transcription endpoint answers one: 413, and a message saying so.  A server that
        sends more than an endpoint accepts has to find that out the way it would in
        production, not by the suite quietly accepting anything.
        """
        self.routes[path] = (status, content_type, _encode(body))
        if refuses_over is not None:
            self.limits[path] = refuses_over

    def fails(self, path: str) -> None:
        """Answer this path with an error, for the promise about a publisher that breaks."""
        self.serves(path, "text/plain", b"the publisher is having a bad day", status=500)

    def _handler(self):
        outside = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._answer()

            def do_POST(self):
                self._answer(self.rfile.read(int(self.headers.get("Content-Length", 0))))

            def _answer(self, request_body: bytes = b""):
                path = self.path.split("?")[0]
                with outside.lock:
                    outside.hits[path] = outside.hits.get(path, 0) + 1
                    known = outside.routes.get(path)
                    if known is None:
                        outside.unknown.append(path)
                # A path this test never described is answered rather than raised on. A
                # server in another language may ask for things the one here does not, and
                # a KeyError inside a handler thread reaches the test as a connection error
                # with no hint of the cause -- where this reaches it as `unknown_paths()`,
                # which says which path and lets a test assert on it.
                status, content_type, body = known or (
                    404, "text/plain", f"the stub was never told what to serve at {path}\n"
                    .encode("utf-8"))
                # The same answer the real transcription endpoint gives, so a server that
                # posts more than it accepts meets the limit here rather than in production.
                limit = outside.limits.get(path)
                if limit is not None and len(request_body) > limit:
                    status, content_type = 413, "application/json"
                    body = json.dumps({"error": {"message": f"body of {len(request_body)} "
                                                f"bytes exceeds the {limit} byte limit",
                                                "code": 413}}).encode("utf-8")
                elif callable(body):
                    body = _encode(body(request_body))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        return Handler


@contextmanager
def podclean_server(outside: Outside, tmp_path: Path, **extra_env: str):
    """The real server as a real process, pointed at the stub and at an empty store.

    ``extra_env`` adds to its environment, for the settings only one test cares about.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ,
           "PODCLEANER_HOST": "127.0.0.1", "PODCLEANER_PORT": str(port),
           "PODCLEANER_BASE_URL": url,
           "PODCLEANER_STORE_ROOT": str(tmp_path / "episodes"),
           "PODCLEANER_TRANSCRIBE_BASE_URL": outside.url,
           "PODCLEANER_LLM_BASE_URL": outside.url,
           "PODCLEANER_LLM_SPEC": "stub-model",
           "PODCLEANER_LLM_API_KEY": "stub", **extra_env}
    proc = subprocess.Popen(SERVER_CMD, env=env, cwd=REPO)
    try:
        _wait_until_listening(proc, url)
        yield url
    finally:
        proc.kill()
        proc.wait()  # reap it here rather than leave a zombie for the rest of the session


def _wait_until_listening(proc: subprocess.Popen, url: str) -> None:
    """Block until the server answers on ``url``, or say why it never will.

    Both failures name themselves.  A server that died is not a server that is slow: one
    that cannot bind a port already taken exits non-zero, which is also how the window
    between this test choosing a port and the subprocess binding it shows up if something
    else takes it first.
    """
    attempts = 50
    for _ in range(attempts):
        if proc.poll() is not None:
            raise RuntimeError(
                f"the server exited with {proc.returncode} before it listened on {url}")
        try:
            requests.get(url, timeout=0.2)
            return
        except requests.ConnectionError:
            time.sleep(0.1)
    raise RuntimeError(f"the server never listened on {url} after {attempts} attempts")
