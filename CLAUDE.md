# PodClean v2

Removes advertising from podcast episodes. Serves a rewritten RSS feed whose enclosure
URLs point back here, and produces the ad-free audio when one is downloaded.

**Current phase: MVP.** Read `docs/spec.md` first. It is the product as a listener meets
it — the two URLs, what comes back, what the audio is guaranteed to be, and what it
deliberately does not do — and **nothing in it names a module, a function or a
constant**. That is the point: it stays true across a refactor, and it is checkable
without reading the source. Where a constant needs a justification, that justification
lives next to the constant in the code, not in a document that has to be kept in step.

`docs/requirements.md` is the same promises with every number taken out, numbered, one
line per thing a black-box test can assert, each naming the test that asserts it. Write
tests against that, not against the spec's prose.

`docs/contract.md` is the third document and the only one that names things: the facts a
port has to match and no listener can see — which models, which endpoints, the words sent
to them, what is written on disk, and the constants that carry a policy. It is what had to
be read out of the Python to build the Elixir, and it is the whole of what the Go was
built from -- written down so the next port need not read any source at all.

## Where things are

    podclean/         the server, Go, one package per thing it does
    tests/            the suite, Python, which knows the server only over HTTP
    tools/            verify and health, which measure the deployed thing from outside

The server was Python, then Elixir, and is Go since 2026-09-18; the suite did not change
across either move, which is the evidence that it really is black-box. Two consequences
worth knowing before you go looking for them: the cut is MP3 frames left out rather than
an ffmpeg re-encode, so nothing in the server shells out; and the store key changed with
the Elixir, so episodes produced before 2026-09-13 are not found and are simply produced
again on first play. Go inherited that key unchanged, so nothing moved on 2026-09-18.

## The one rule

Never remove real content. A missed advertisement is an annoyance. A sentence clipped
mid-word is destructive and the listener cannot recover it. When a cut edge is uncertain,
choose rather more advertisment then less (so some seconds of advertisement at the begin of the ad and at the end is tolerateable, and even good because i can better see if it was really advertisement that was clipped). This rule outranks every other goal, including
in the MVP.

## Commands

There is no `python` and no `make` on this host. Use `./run`.

    ./run verify <feed> <guid>   # THE acceptance measure: ad seconds removed, programme
                                 # frames lost, by frame walk against the publisher master
    ./run health     # is the deployed thing actually working: process, code==HEAD,
                     # public route, feed rewrite, last verdict
    ./run test       # THE suite: the routes end to end on an episode it builds itself
                     # (73 min of silence at a real one's timings), ~45 s, Docker.
                     # Imports nothing from the server; it knows it only as a process on
                     # a port. PODCLEAN_SERVER_CMD=... runs it against an implementation
                     # in any other language -- which is how the server came to be Elixir
                     # and then Go, and the suite did not change either time. The contract
                     # is the docstring of tests/integration/support.py.
    ./run build      # build the image the server runs in. The binary is compiled into
                     # it, so every .go edit needs this before a restart -- ./run health
                     # tells you which of the two you are missing.
    ./run serve      # run the server, in that image, on the host's network
    ./run save       # green suite, then stage everything

An unknown command exits 2 rather than printing the usage banner and succeeding.
GitHub Actions runs `./run test` on every pull request and on master
(`.github/workflows/test.yml`): same command, a venv built to sit where `./run` looks for
it, and two images — the server's, via `./run build`, and an ffmpeg for the suite alone,
built from Debian 12 because that is the ffmpeg this host runs. The pin is about the
ruler, not about memory: the suite encodes the episode it plays and decodes what came
back, and another ffmpeg can lay those frames out differently, so CI would be measuring a
file this host would never have made. (The Alpine OOM kill that used to justify the pin
was the old Python cutter's, at 256 MB and still at 512 MB. Nothing re-encodes now, so
that failure cannot recur — but do not take the pin out on those grounds.) All eighteen
tests run there and nothing skips. `./run verify` stays host-only: it is the one thing
that still needs the commercial episode.
`./run whisper`, `./run labels`, `./run ads` and `./run record` no longer exist: local
whisper.cpp transcription was deleted (one paid API request now), and the hand-labelled
ad-scoring suite was deleted because `./run verify` measures the same thing exactly,
against the publisher's own master, with no labels to maintain.
`./run mutate` and `mutations.tsv` went on 2026-09-12, at the owner's call: the gate
checked that the tests could fail, but nobody could read it, and an unreadable gate is
one nobody will keep honest. Do not rebuild it.

## Host limits

4 GB RAM; Caddy, Vikunja and Portainer hold the rest.
There is no `ffmpeg`, `ffprobe` or `python` on `PATH`; ffmpeg runs only inside the
`whisper-cpp:local` Docker image (the name is legacy twice over -- from before whisper.cpp
was deleted, and from before the server stopped needing ffmpeg at all). The **suite** runs
it, to build the episode it plays and to measure what came back; `PODCLEAN_TEST_FFMPEG_IMAGE`
points it elsewhere. The server does not: it cuts by leaving whole MP3 frames out, in
process. One heavy job at a time; three background jobs were already killed here for
memory.

## Never commit

`var/` (823 MB of commercial audio under `var/fixtures/`, plus `var/cache/`,
`var/transcripts/` and `var/reports/`) and `.secret.json`.
The OpenRouter token lives in `.secret.json` under `openrouter-token`.

## How to work here

- Simplicity wins! Use Abstractions, one layer should be responsible for one thing. The buisnesslogic should be decoupled from the low-level logic as much as possible. Build useful classes, types, functions that nicely work toghether. I think the pipeline pattern makes sense here.
- Do not build for cases that have not happened.
- Commit at every green test run. Small commits on a branch are cheap.
- Delegate broad searches to a subagent so its hits never enter the main context.
- Change files with Edit, not with shell scripts that pipe whole files through the context.
- Batch independent tool calls into one response.
- Clean up after every step: delete what the step made unnecessary, then commit.

## How a change is made

1. **No unit tests while the MVP phase lasts.** The internals are going to be
   refactored, and a test naming `snap_segment` or `plan_cuts` has to be rewritten when
   they move -- so it pins the shape of today's code rather than what a listener gets.
   Everything is proven through the four routes in `tests/integration/`, against local
   servers standing in for the origin and the two paid endpoints. A test only if it would
   have caught this bug.
2. Work testdriven. so on every change. first check if this change touches an existent Test. If not create a Test that verifies the correct behavoir. It should first run red. then change the implementation, then it should run green. At MVP phase i expect only integration tests to be created or changed. fokus on simple meaningful tests not many complex
5. **`./run test` green**, then commit and PR.

**When you catch yourself** adding a test that pins a literal value in both directions,
or writing a paragraph of document where a line of `docs/requirements.md` would do: stop.
That is the failure mode this project has had three times. A requirement earns its number
by being one thing a black-box test can assert, with no number in its text.

`gh` is authenticated on this host (as `Valleeh`) and pushing over SSH works.
