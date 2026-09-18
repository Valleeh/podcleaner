# PodClean

Subscribe to a podcast through PodClean instead of through its publisher, and the episodes
your podcatcher downloads are the publisher's own — with the advertising cut out.

Same show, same episode list, same titles, show notes and publication dates. The audio is
the publisher's own encoded bytes with whole advertising breaks left out. Nothing is
re-encoded, so what you hear is what they recorded.

## The one rule

**No second of programme is ever removed.**

A missed advertisement is an annoyance. A sentence that disappears cannot be recovered,
and the listener cannot even tell it is gone. Every trade-off resolves that way, and the
price is real: a good part of the advertising survives — between a third and a half, on
the episodes measured. A break that cannot be placed on the exact words the model quoted
is left in. A plan that is not believable as a whole is thrown away and the episode is
served untouched.

That is measured, not asserted. Publishers who stitch advertising in serve the ad-free
master to a plain HTTP client, so the served file and that master can be walked frame by
frame against each other. `./run verify` does exactly that, shares no code with the part
that does the cutting, and reports both numbers:

```
$ ./run verify https://feeds.lagedernation.org/feeds/ldn-mp3.xml 91b5c148-…

advertising in the file the listener would have got: 191.664 s in 3 break(s)
  1  01:09.84 - 02:12.01  ( 62.172 s)   removed   0.000 s
  2  24:25.74 - 25:57.18  ( 91.440 s)   removed  67.300 s
  3  75:24.26 - 76:02.32  ( 38.052 s)   removed  23.660 s

PROGRAMME INTACT   every removed interval lies inside a true ad
ADVERTISING        90.960 s of 191.664 s removed (47.5%), 100.704 s still plays
```

## The URLs

| | |
|---|---|
| `GET /rss?feed=<publisher feed url>` | the publisher's feed, byte for byte, with three links per episode changed |
| `GET /podcast?feed=…&guid=…` | the episode's audio, produced on the first play |
| `GET /chapters?feed=…&guid=…` | chapter marks, timed against the audio this server serves |
| `GET /transcript?feed=…&guid=…` | the transcript as WebVTT, on the same timeline |

**The feed** is the publisher's document with every episode's audio link, its chapter link
and a transcript link pointing here, and nothing else changed — not the titles, not the
show notes, not the encoding. Put the original links back and the two documents are
identical. A feed that does not declare the podcast namespace gets no chapter or
transcript link, and an episode whose identifier another episode shares keeps the
publisher's link, because a link to the wrong episode is worse than none.

**The first play** of an episode takes three to five minutes and costs a few cents,
rising with the episode's length. The connection is held until the file is whole; no
partial file is ever sent. Every play after that is instant and returns byte-identical
audio: an episode is produced once, never re-examined, never re-billed. An episode no feed
fetched here has named is 404, and no request goes out for it. A publisher that fails, or
answers with something that is not audio, is a 502 and nothing is kept.

**The chapter marks and the transcript** come out of the same pass that finds the
advertising, so they cost no second reading. Both answer 404 until the episode has been
played once, and asking for them never starts that work. Every time in them is on the
served timeline, moved earlier by exactly the audio removed before it. A chapter mark that
fell inside a break is dropped rather than moved to the join, and a transcript line that
overlapped a cut is dropped whole rather than trimmed, so neither ever claims words that
are not in the file. A cut episode carries the same marks inside the MP3 as well, because
some podcatchers read those and ignore the feed's link.

## What you get when you play it

* The publisher's own audio, minus whole advertising breaks. **A cut begins 1.5 s after
  the break's first word and ends 1.5 s before the break's last cue ends**, so the join
  lands inside advertising and you hear a moment of every break at each end. The margin
  is where an error of a word lands, and a word of advertising left in is an annoyance
  where a word of programme removed is a loss.
* A file shorter by exactly what was removed, to within one MP3 frame: the cut leaves
  whole frames out and re-encodes nothing.
* No cover art, even where the publisher's file had it.

## What it does not do

* **The show's own promotion is left in** — its live dates, its other podcast, its
  outro. Only paid reads, host endorsements and trailers for other shows are cut.
* **A few seconds of every break survive** at each edge, by the margin above; where the
  transcriber's cue runs a breath past the break, that breath survives too.
* **A break the model is not sure about is left in.** So is every break of a plan that is
  not believable as a whole — one break implausibly long, or breaks adding up to an
  implausible share of the episode — and the episode is then served untouched.
* **Whole breaks are sometimes missed.**
* An episode longer than about two hours twenty is served untouched.
* Once an episode has been produced it is never revisited, so an improvement made later
  does not reach episodes already fetched. To have one produced again, delete its
  `audio.mp3`.

## Run it

Needs Docker and an [OpenRouter](https://openrouter.ai) token. Nothing else: the server
has no ffmpeg, no database and no runtime dependencies.

```sh
cp .env.example .env          # set PODCLEANER_BASE_URL and PODCLEANER_LLM_API_KEY
docker compose up -d --build
```

Put a reverse proxy in front of it — the episodes you produce are yours, not the
internet's — then paste `https://your-host/rss?feed=<the publisher's feed url>` into your
podcatcher. Episodes already downloaded under the publisher's feed keep playing from
there; only what appears after the switch comes through here.

Produced episodes live in the `episodes` volume, one directory each, roughly the size of
the original. Nothing deletes them.

## Working on it

```
./run test                      the suite, needs Docker
./run build                     build the image the server runs in
./run serve                     run it, in that image, on the host's network
./run health [feed url]         is the deployed thing working
./run verify <feed> <guid>      was a cut right, against the publisher's ad-free master
```

The suite knows the server only as a process on a port. It starts the real binary, points
it at a local stub standing in for the publisher and the two paid endpoints, and asserts
on what a listener gets. It builds its own 73-minute episode, decodes what comes back with
an ffmpeg that shares no code with the server, and skips nothing. `PODCLEAN_SERVER_CMD=…`
points it at a server in any other language. GitHub Actions runs it on every pull request.

Two more documents, and neither repeats this one:

* **[docs/requirements.md](docs/requirements.md)** — the promises above with every number
  taken out, numbered, one line per thing a black-box test can assert, each naming the
  test that asserts it.
* **[docs/contract.md](docs/contract.md)** — what a re-implementation has to match and no
  listener can see: which models, which endpoints, the words sent to them, what is written
  on disk, and the constants that carry a policy.

## License

MIT. See [LICENSE](LICENSE).
