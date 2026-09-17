# PodClean

Subscribe to a podcast through PodClean instead of through its publisher, and the episodes
your podcatcher downloads are the publisher's own — with the advertising cut out.

Same show, same episode list, same titles, same show notes, same publication dates. The
audio is the publisher's own encoded bytes with whole advertising breaks missing. Nothing
is re-encoded, so what you hear is what they recorded.

## The one rule

**No second of programme is ever removed.**

A missed advertisement is an annoyance. A sentence that disappears cannot be recovered,
and the listener cannot even tell it is gone. Every trade-off in this project resolves
that way, and the price is not small: **between a third and a half of the advertising
survives.** A break that cannot be placed on the exact words the model quoted is left in.
A plan that is not believable as a whole is thrown away and the episode is served untouched.

That is the deal, and it is measured rather than asserted. Publishers who stitch
advertising in serve the ad-free master to a plain HTTP client, so the served file and
that master can be walked frame by frame against each other. `./run verify` does exactly
that, shares no code with the part that does the cutting, and reports both numbers:

```
$ ./run verify https://feeds.lagedernation.org/feeds/ldn-mp3.xml 91b5c148-…

advertising in the file the listener would have got: 191.664 s in 3 break(s)
  1  01:09.84 - 02:12.01  ( 62.172 s)   removed   0.000 s
  2  24:25.74 - 25:57.18  ( 91.440 s)   removed  67.300 s
  3  75:24.26 - 76:02.32  ( 38.052 s)   removed  23.660 s

PROGRAMME INTACT   every removed interval lies inside a true ad
ADVERTISING        90.960 s of 191.664 s removed (47.5%), 100.704 s still plays
```

The first break survived because the model quoted a single word as the break's last words,
and one word cannot place a cut edge. That is the rule working, not failing.

## The URLs

| | |
|---|---|
| `GET /rss?feed=<publisher feed url>` | the publisher's feed, byte for byte, with three links changed |
| `GET /podcast?feed=…&guid=…` | the episode's audio, produced on the first play |
| `GET /chapters?feed=…&guid=…` | chapter marks, timed against the audio this server serves |
| `GET /transcript?feed=…&guid=…` | the transcript as WebVTT, same timeline |

The first play of an episode takes three to five minutes and costs a few cents; the
connection is held until the file is whole. Every play after that is instant and returns
byte-identical audio. Nothing is ever re-examined or re-billed.

The chapter marks and the transcript fall out of the same pass that finds the
advertising — it reads the whole episode either way — so they cost a few hundred extra
words of its answer and no second reading. A cut episode carries the same marks inside the
MP3 as ID3 chapter frames, because Overcast reads those and ignores the feed's link.

## Run it

Needs Docker and an [OpenRouter](https://openrouter.ai) token. Nothing else: the server
has no ffmpeg, no database and no runtime dependencies.

```sh
cp .env.example .env          # set PODCLEANER_BASE_URL and PODCLEANER_LLM_API_KEY
docker compose up -d --build
```

Put a reverse proxy in front of it — the episodes you produce are yours, not the
internet's — then paste `https://your-host/rss?feed=<the publisher's feed url>` into your
podcatcher.

Produced episodes live in the `episodes` volume, one directory each, roughly the size of
the original. Nothing deletes them. To have an episode produced again — after an
improvement, or because a cut was wrong — delete its `audio.mp3`.

## How it works

Downloading the episode as a podcatcher would, because publishers who stitch advertising
in serve something different to everyone else. Then: the bytes must parse as MP3 frames or
it is not an episode, whatever the reply calls itself. Transcription, in pieces split on
whole frame boundaries and put back on one timeline. One model pass that returns both the
advertising and the chapter marks. Then the part that matters — every break is placed on
the exact words the model quoted, found exactly once, and a cut runs from 1.5 s after the
break's first word to 1.5 s before its last, so the join always falls inside advertising.
Finally the cut itself: whole MP3 frames left out, nothing re-encoded, nothing shelled out
to.

Eleven small Go packages, one job each, and the one that decides what may be removed
talks to nobody.

## Tests

```sh
./run test        # 18 tests, about 40 seconds, needs Docker
```

The suite knows the server only as a process on a port. It starts the real binary, points
it at a local stub standing in for the publisher and the two paid endpoints, and asserts
on what a listener gets. It imports nothing from the server — it even measures the length
of the served audio by decoding it with ffmpeg, so the ruler and the thing measured share
no code. It builds its own 73-minute episode, so nothing skips and no fixture has to be
downloaded. GitHub Actions runs the same command on every pull request.

`PODCLEAN_SERVER_CMD=…` points it at a server in any other language. That is not
hypothetical: this server has been Python, then Elixir, then Go, and the suite did not
change.

## The documents

Three, with three jobs, and none of them repeats another.

* **[docs/spec.md](docs/spec.md)** — the product as a listener meets it. The two URLs, what
  comes back, what the audio is guaranteed to be, and what it deliberately does not do.
  Names no module, no function and no constant, so it stays true across a rewrite.
* **[docs/requirements.md](docs/requirements.md)** — the same promises with every number
  taken out, numbered, one line per thing a black-box test can assert, each naming the test
  that asserts it.
* **[docs/contract.md](docs/contract.md)** — the rest: which models, which endpoints, the
  words sent to them, what is written on disk, and the constants that carry a policy. What
  a re-implementation has to match and no listener can see.

## License

MIT. See [LICENSE](LICENSE).
