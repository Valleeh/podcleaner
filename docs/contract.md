# What a port must match

`README.md` is what a listener can check and `docs/requirements.md` is that as
assertions. This document is the rest: the facts a re-implementation has to match and no
listener can see — which third parties it talks to, the exact words it says to them, what
it writes on disk, and the constants that carry a policy. The suite proves almost none of
it: its stub answers whatever it is asked and looks at no file the server writes, so a
port that got every identifier, header and layout below wrong could still pass
`./run test`. It has been used for exactly that once: a port built from these documents
and the suite passed without its author reading the server it replaced.

The values are here; the reason a value is what it is lives next to it in the code, which
is the only place that cannot drift from it. Each section names its home under
`podclean/`.

## Configuration

Everything is an environment variable, read at use, no configuration file. `internal/config`.

| variable | default | what it is |
|---|---|---|
| `PODCLEANER_HOST` | `0.0.0.0` | address to listen on |
| `PODCLEANER_PORT` | `8080` | port to listen on |
| `PODCLEANER_BASE_URL` | `http://127.0.0.1:$PODCLEANER_PORT` | what it calls itself in the links it writes into a feed |
| `PODCLEANER_STORE_ROOT` | `var/episodes` under the working directory; the image sets `/var/lib/podclean`, a volume, and `./run serve` sets `$PWD/var/episodes` | one directory per episode below this |
| `PODCLEANER_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-shaped; `/chat/completions` is appended |
| `PODCLEANER_TRANSCRIBE_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-shaped; `/audio/transcriptions` is appended |
| `PODCLEANER_LLM_API_KEY` | empty | bearer token for **both** of the above |
| `PODCLEANER_LLM_SPEC` | `cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash` | which model or models classify |
| `PODCLEANER_TRANSCRIBE_MAX_BYTES` | `25165824` (24 MiB) | largest body sent to the transcriber |
| `PODCLEANER_PARENT_PID` | unset | if set, halt when `/proc/<pid>` disappears — how `./run serve` dies with its `docker run` |

`./run serve` reads the token out of `.secret.json` under `openrouter-token` when the
environment has none.

## What it asks of other people

`internal/outside`. Nothing retries: a second ask is a second bill. Only HTTP 200 is a
success; anything else is a failure carrying `<url> answered <status>: ` and the first
200 bytes of the body. From the publisher that is a 502 to the listener. From a paid
endpoint, once the audio is in hand, it is a `failed` verdict with that text in `error`,
and the listener is served the publisher's own audio with a 200.

**The publisher's audio.** `GET` the enclosure URL with `user-agent: AntennaPod/3.6.0`
and a 600 s receive timeout, body not decoded. The user-agent is not cosmetic: publishers
who stitch advertising in serve the ad-free master to plain clients and the stitched copy
to podcatchers, so a request that did not look like a podcatcher would fetch audio no
listener is ever served, and `./run verify` would compare a file against itself.

**The publisher's feed.** `GET`, 120 s, *without* the podcatcher user-agent: the document
is the same for every client.

**The publisher's chapter marks**, where the feed named any: `GET`, default headers,
expecting `{"chapters": [{"startTime": <seconds>, "title": <string>}]}`. Anything else,
or any failure, is silently no hint — a courtesy, never a dependency.

**Transcription.** `POST $PODCLEANER_TRANSCRIBE_BASE_URL/audio/transcriptions`, bearer
auth, 900 s, `multipart/form-data`:

| field | value |
|---|---|
| `file` | the audio, filename `episode.mp3`, content type `audio/mpeg` |
| `model` | `openai/whisper-large-v3-turbo` |
| `response_format` | `verbose_json` |
| `timestamp_granularities[]` | `segment` **and** `word`, sent twice |
| `language` | the first two letters of the feed channel's `<language>`, lowercased; absent when the feed names none |

Both granularities are required. The reply must carry top-level `segments` (each with
`start`, `end`, `text`) and `words` (each with `start`, `end`, `word`); a reply with only
segments is rejected. Segments become the numbered cues the model answers about; the
words are what a cut's start is placed on.

**Classification.** `POST $PODCLEANER_LLM_BASE_URL/chat/completions`, bearer auth, 900 s,
JSON body: `model`, `messages`, `temperature: 0`, `response_format: {"type":
"json_object"}`. Nothing else is sent — no `max_tokens`, no reasoning switch — so whether
a model reasons is the provider's default for that model id; the reply's `usage` block is
not read or logged, so what an episode's transcription and its model calls each cost is
not on record. `messages` is the system prompt and one user message of up to three parts,
separated by blank lines and each absent with its separator when empty: the publisher's
chapter marks under the line `The publisher's own chapter marks for this episode, as a
starting point. They may be wrong and they do not say where the advertising is:` as one
`m:ss title` per mark; the screening models' findings under their header (below); and the
whole rendered transcript — nothing chunks it, and the model's context window is the only
limit past the episode-length cap. Read back `choices[0].message.content`, strip a ``` or
```json fence if the model wrapped one round it, parse as JSON, and require a `segments`
list. Anything else is an unreadable reply: not asked again now, and not a decision about
the audio either — `verdict.json` is written with `state: failed` and the error, nothing
else is, and the next play asks again.

## The cascade

`internal/classify`. `$PODCLEANER_LLM_SPEC` is `cascade:screen[+screen]>verifier`, or a
bare model id for one pass. Every screening model reads the transcript, with the
publisher's marks; their segments are then listed to the verifier as
`cues <a>-<b> (<category>): <reason>` under

    A first pass reported these; verify each and add any it missed:

and only the verifier's answer is used. A screen that fails or answers rubbish is dropped
silently — it can only ever have added candidates.

## The prompt

`internal/classify/prompt.go`. It is the only place the boundary rules are stated, and
the model's obedience to them is what the one rule rests on, so a port carries it over
word for word. For a port that cannot, what it must contain:

* the transcript's format, `[cue] m:ss text`, one line per cue, and that it may be German;
* the answer schema below, and "answer with the JSON object only";
* the five categories — `sponsor_read`, `host_endorsement`, `cross_promo`, `self_promo`,
  `credits` — each defined, because only the first three are ever cut. `self_promo` has
  to name anything the same people make, another podcast they publish included, or the
  model files the hosts' own second show under `cross_promo` and it is cut; and it has
  to stop there -- only what the hosts say is theirs, and a regional spot the publisher
  inserted, in another language and with no announcer, is `sponsor_read`, or the model
  files it under `self_promo` at 0.6 and it is left in;
* the German words a break announces itself with — `Werbung`, `Anzeige`, `präsentiert
  von`, `und jetzt zurück zur Sendung` — next to the English ones, because the episodes
  this is measured on are German;
* **cue numbers, never timestamps**;
* boundaries tight, from the break's first cue to its last, never the host's lead-in and
  never on into the programme after it; that the cut ends where `end_cue` ends; and that
  a break can be one cue, or part of one;
* `first_words` copied **exactly**, three to six words, taken from within
  `start_cue`..`end_cue` because the quote is searched only in the words of the cues the
  segment names, and the break's own words even where a cue holds the end of the host's
  sentence too. This is a mechanical check and has to be stated as one — a count, the
  consequence, and a worked example of a one-word quote thrown away. Stated as a
  preference it is ignored and most of the model's correct answers are wasted;
* **where advertising hides**, and that it very often follows the hosts' goodbye: a
  model that stops reading at the sign-off reports no post-roll;
* stacked ads are separate segments; hand-off and return phrases belong to the break;
* confidence is an honest estimate that the run is promotional *and* its boundaries
  right;
* 4 to 20 chapters over the editorial content, titled in the episode's language, under
  60 characters, never inside a run reported as promotional.

How well a prompt does this is measurable: mark the advertising in a handful of episodes
by hand, then count the breaks that survive every mechanical check. Two rules that were
tried and made it worse are recorded next to the prompt so that nobody adds them back.

The answer, one JSON object:

    {"segments": [{"start_cue": int, "end_cue": int, "category": string,
                   "confidence": number 0-1, "reason": string, "first_words": string}],
     "chapters": [{"cue": int, "title": string}]}

## Sending an episode up in pieces

`internal/mp3`, `File.Pieces`. A full episode is several times the transcriber's limit.
It is split on whole MP3 frame boundaries into pieces of at most
`PODCLEANER_TRANSCRIBE_MAX_BYTES`, each transcribed on its own; the cues that come back
are shifted by the piece's start time and **renumbered continuously from 1 across all
pieces**. The renumbering is load-bearing: a break is placed by looking its cues up by
number. No overlap is added between pieces: a garbled word at a boundary can only make a
quote unfindable, which leaves an ad in. 24 MiB against the endpoint's 25: the multipart
wrapper goes up too, and a request refused for being a few hundred bytes over costs the
whole episode.

Then every **hole** is sent up again: a stretch of at least the constant below,
including before the first cue and after the last, that no cue and no word with a
duration covers. It goes as the frames starting inside it, split the same way. Its cues
and words are shifted by its own start and the whole transcript is put in time order
before it is numbered. Zero-length words that an earlier reply left inside a hole a
later reply filled, both ends included, are dropped: they are the lost words parked at
its far end, and would otherwise be there twice. The holes that remain are asked about
again, round after round, up to the limit below; a round in which no request came back
ends it. A hole whose request fails or comes back empty stays a hole and is not an error.

## Cues, and how a break is placed on them

`internal/transcript`, `internal/plan`.

* One cue per transcription segment, numbered from 1, with the segment's own start and
  end. Each word is attached to the last cue that had started by the word's own start.
* **The last cue, and only the last cue, is extended to where its words end** when that is
  later than the segment's own end, capped at the length of the audio; it is never
  shortened. A transcription reply does not agree with itself: its segments stop while
  the words timed inside them keep going. At the end of the file that is a post-roll the
  last segment stopped short of, and a cut ending at the segment's stated end would leave
  most of it in. In the middle it is the aligner having dropped a stretch of speech and
  parked the words after it at the far end of the hole — programme, unmentioned by any
  cue — so every other cue ends exactly where the transcriber said. A word with no
  duration is never followed, and if the length of the audio is unknown the last cue
  does not grow: a missing bound must not resolve towards cutting.
* The model sees `[<number>] <m:ss> <text>` per line, minutes unpadded, seconds to two
  digits.
* A segment is considered only if its category is one of the three that are cut and its
  confidence is at least the threshold below.
* Both cue numbers must exist and be in order. An invented number refuses the segment
  outright rather than narrowing it: the cut runs to the end of the last cue named, and a
  number clamped to the last cue there is would run it to the end of the episode.
* **The start is placed on the quoted first words**, matched against the words of the
  named cues: compared as bare letters and digits, case folded, everything else stripped,
  Unicode-aware so that umlauts are letters. A transcript word with no letter or digit in
  it is dropped from the sequence first, so a quote matches across standalone
  punctuation. The quote's letters and the words' letters are compared run together,
  starting and ending on a word boundary, so "80,000" in the quote matches the words
  `80` `,000`. At most six tokens of the quote are used, from the front. Found, the first
  matched word's start is the break's start; found more than once, the last place it is
  found is used, which removes the least. Not found, the first token is dropped and it is tried
  again, down to three tokens — every retry moving the start later, never earlier. Fewer
  than three tokens is refused.
* **The end is where the last named cue ends.** No quote is asked for it: the cue that
  opens a break usually opens with the host still talking, but the cue that closes one
  closes with it.
* A break whose end is not after its start — a first word stamped past the last cue's
  end — is refused; so is one shorter than two margins.
* A cut runs from the start plus one margin to the end less one margin. Cuts are then
  sorted, overlapping ones merged, and the whole plan judged.

## The constants that carry a policy

| value | where | what it decides |
|---|---|---|
| 1.5 s | `plan.margin` | kept inside each edge of a break |
| 6 / 3 | `plan.maxTokens` / `plan.minTokens` | how much of a quote is matched on, and how short it may wear down to |
| 0.5 | `plan.minConfidence` | below this the model's segment is ignored |
| 600 s | `plan.longestBreak` | a single cut longer than this refuses the whole plan |
| 10 s | `episode.shortestHole` | a stretch with no words at least this long is transcribed again |
| 3 | `episode.holeRounds` | how many rounds of holes are asked about |
| 0.2 | `plan.mostOfAnEpisode` | cuts totalling more than this share of the episode refuse the whole plan |
| 3 s | `mp3.minimumSeconds` | fewer seconds of parsable frames and the publisher's reply is not audio |
| `sponsor_read`, `host_endorsement`, `cross_promo` | `plan.cuttable` | the only categories ever cut |

A plan's outcome is one of four states, written into `verdict.json`: `cut` (at least one
candidate was placed; a refused sibling is recorded in `error`), `clean` (nothing the
model proposed was a cuttable category at or above the confidence threshold — `proposed`
may still be non-empty), `refused` (every such candidate was refused, or the plan as a
whole was not believable), `failed` (a stage did not finish). Only `failed` is retried on the next play.

## The cut itself

`internal/mp3`. Nothing is re-encoded and nothing is shelled out to.

* The file is walked as MPEG 1/2/2.5 Layer III frames, each `{offset, length, seconds}`.
  A sync word is only trusted when the frame it predicts is followed by another parsable
  header, or by the end of the file.
* A leading ID3v2 tag is skipped by its syncsafe size, plus 10 more bytes when the footer
  flag is set. A first frame containing `Xing` or `Info` is dropped: its counts would
  describe the file before the cut.
* A frame is removed when **its start** lies in a cut, half open at the end. So a cut
  lands on a frame edge — 26 ms at 44.1 kHz — and everything served is the publisher's
  own bytes. A replay is byte-identical by construction.
* The duration of the episode is the sum of the frame durations, not anything a header
  claims. Whether a reply is audio at all is the same question: the bytes must parse as
  at least the minimum above of MP3 frames, or it is not an episode however it describes
  itself.

## What it writes on disk

`internal/store`. One directory per (feed, episode):

    $PODCLEANER_STORE_ROOT/<first 32 hex characters of sha256(trim(feed) <> 0x00 <> trim(guid))>

The NUL byte between the two is what stops a feed URL ending in an episode id from
colliding with another pair. `tools/verify.py` derives the same key independently, so a
port that changes it silently breaks the one tool that can prove the one rule.

| file | written | what it is |
|---|---|---|
| `source.json` | when a feed names the episode | `{"url": <publisher enclosure>, "feed": <feed url>, "chapters_url": <publisher's marks or null>, "language": <two letters, absent when the feed names none>}`. Its presence is what makes an episode playable: no `source.json`, 404, and nothing goes out. |
| `audio.mp3` | first play | when cut: the ID3 chapter tag (only if there are marks), then the kept frames — the publisher's own tag, Xing/Info frame and any bytes between frames are gone. Otherwise the publisher's bytes exactly as fetched. |
| `chapters.json` | first play | the marks on the served timeline |
| `transcript.vtt` | first play | the transcript on the served timeline |
| `verdict.json` | first play, always, last | what was decided |

Every file is written to `<name>.tmp` and renamed, so a reader sees it whole or not at
all. Audio first, documents next, verdict last — a verdict is the record that the work
finished. When the state is `failed`, only `verdict.json` is written, so the next play
retries. A failure after the audio was in hand (transcription, classification) serves the
publisher's own bytes meanwhile, out of memory; a failed fetch, or a reply that is not
audio, is answered 502.

`verdict.json` carries `schema` (`"podclean.verdict/1"`), `state`, `error` (the refusals
joined with `; `, on a `cut` verdict too), `model_spec` (the configured spec, whether or
not a model answered), `proposed` as `[{start_cue, end_cue, category, confidence}]`,
`proposed_count`, `cues`, `duration_seconds`, `removed_seconds`, `removed` as
`[[start, end], ...]`, `chapters` as `[[at, title], ...]`, and `source_sha256` over the
bytes that were fetched — what `./run verify` uses to notice that the publisher has
re-stitched the episode since, in which case it refuses to compare. Every seconds value is
rounded to three decimals.

One heavy job at a time: production is taken under a global lock, and a second play of
the same episode waits for the first rather than paying again.

## What it serves

`internal/web`. Four routes, everything else 404 `not here`. Every route answers 400 when
a parameter it needs is missing or blank.

| | |
|---|---|
| `GET /rss?feed=` | 200 `application/rss+xml`; 502 with the publisher's failure text |
| `GET /podcast?feed=&guid=` | 200 `audio/mpeg`, **no charset**; 206 with `content-range` for a range; 404 for an episode no feed has named; 502 if the publisher failed |
| `GET /chapters?feed=&guid=` | 200 `application/json+chapters`; 404 until the document exists |
| `GET /transcript?feed=&guid=` | 200 `text/vtt`; 404 likewise |

`HEAD` is answered as the `GET` would be, without a body — a podcatcher asks it before it
queues a download, and a 404 leaves the episode stuck at "waiting to download" forever.
The audio reply carries `accept-ranges: bytes`; the feed and the two documents ignore a
range and are always whole. One range is understood, in all three forms (`a-b`, `a-`,
`-n`); several ranges, an unparsable one, or one starting past the end are answered with
the whole file. Every request is logged as `from=<address> agent=<user-agent> <METHOD>
<URI> status=<n> bytes=<written> range=<the Range header as sent>`.

Two error bodies are worth keeping recognisable because they answer the question a
reader has: `unknown episode: no feed fetched by this server has named it`, and `not
produced yet: written when the episode is first played`.

## The formats it writes

**`chapters.json`** — the podcast namespace's own format, `{"version": "1.2.0",
"chapters": [{"startTime": <seconds, float>, "title": <string>}]}`. Seconds, not
milliseconds.

**`transcript.vtt`** — `WEBVTT`, then one block per cue with text, numbered from 1 on the
served timeline, `HH:MM:SS.mmm --> HH:MM:SS.mmm`.

**In the audio itself** — an ID3v2.4 tag ahead of the frames, holding one `CTOC` (element
id `toc`, top-level and ordered, listing every chapter id) and one `CHAP` per mark
(element id `ch1`, `ch2`, …, start and end in milliseconds as 32-bit big-endian, both
byte offsets `0xFFFFFFFF`, one embedded `TIT2` with a UTF-8 encoding byte). All frame
sizes syncsafe. The last chapter ends at the cut episode's own duration. Written only for
an episode that was actually cut and has at least one mark — with none there is no tag at
all; every other episode keeps whatever the publisher's own file carried. It exists
because Overcast displays chapter marks from the file and ignores the feed's link, and
Apple Podcasts reads the file first.

**Both timelines** are the same arithmetic in one place (`internal/timeline`), so the two
cannot disagree: every time is moved earlier by exactly the audio removed before it. A
transcript line that overlaps a cut at all is dropped whole rather than trimmed, and a
chapter mark whose moment was removed — inside a cut, which is the break less its
margins, not merely inside the break the model reported — is dropped rather than moved to
the join.

## Rewriting a feed

`internal/feed`. The document is **never parsed and re-serialised** — that is what makes
"nothing else changed" true byte for byte, CDATA, entities and whitespace included. The
only edits are made by regex, inside `<item>` elements:

* the `url` attribute of `<enclosure>` becomes `<base>/podcast?feed=…&amp;guid=…`;
* where the feed declares `https://podcastindex.org/namespace/1.0` under any prefix, the
  first chapters element and the first transcript element of that prefix in the item are
  repointed the same way, url and type, or one is inserted immediately before `</item>`
  if the publisher had none, typed `application/json+chapters` and `text/vtt`; any further
  ones are left as the publisher wrote them. No namespace declared, no document links;
* the query is URL-encoded and its separator written `&amp;`, because it is going into XML;
* the guid is the raw text between its tags, CDATA markers stripped and trimmed, entities
  left as written.

An item with no guid, no enclosure, or a guid that another item in the same feed also
carries is left exactly as the publisher wrote it. A feed in which no item at all can be
bound is answered 502.

## The tools outside the server

`tools/verify.py` is the acceptance measure and the only thing that can prove the one
rule. It fetches the publisher's enclosure twice — with the podcatcher user-agent, which
is the stitched file this server cut, and with a plain client's, which is the ad-free
master — walks the two frame by frame to recover the inserted breaks exactly, and checks
the intervals `verdict.json` says were removed against them. The served file itself is not
fetched: what is proven is the plan. It refuses to answer when `source_sha256` says the
publisher has re-stitched since, and when the two fetches are the same bytes there is
nothing to measure and it says so.

`tools/health.py` knows the `./run serve` deployment: it checks that its `docker run` is
running, that the image's `org.podclean.source` label equals a sha256 over
`podclean/**/*.go` and `go.mod` — a port in another language has to change that filter or
the check proves nothing — and that a container of that image is up; that the server
answers locally; that the public address, read from that process's environment, answers;
that a feed named on the command line still comes back rewritten (without one the check
is skipped and counted as passed); and that the newest `verdict.json` has a state other
than `failed`. It pins three things no other document states: `/podcast` with no query
answers exactly 400 (how it tells "alive and validating" from "dead"); the public address
answers 400 or 401 (401 being the basic auth in front of it); and `/rss?feed=` answers 200
with `/podcast?` somewhere in the body.

So three programs share these names, and renaming one silently breaks a tool rather than
the server: the store directory, `verdict.json`'s `state`, `removed`, `removed_seconds`
and `source_sha256` (bare lowercase hex, no `sha256:` prefix), `source.json`'s `url`, and
the podcatcher user-agent, which `tools/verify.py` re-fetches with and must equal the
server's.
