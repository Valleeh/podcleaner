# What a port must match

`docs/spec.md` is what a listener can check and `docs/requirements.md` is that as
assertions. This document is the rest: the facts that are **not** observable from outside
and that had to be read out of the source when the server was rewritten from Python into
Elixir on 2026-09-13 -- which third parties it talks to, the exact words it says to them,
what it writes on disk, and the constants that carry a policy. The suite proves almost none of
it: its stub answers whatever it is asked and looks at no file the server writes, so a
port that got every identifier, header and layout below wrong could still pass `./run test`.

So this is the checklist for the next implementation, and the place to look when a cut
goes wrong in production but not in the suite. It has been used once for exactly that: the
Go server was built from these three documents and the suite, without its author reading
the Elixir it replaced.

The values are here; the reason a value is what it is lives next to it in the code, which
is the only place that cannot drift from it. Current home of each fact is named in
`podclean/`.

## Configuration

Everything is an environment variable, read at use, no configuration file. `internal/config`.

| variable | default | what it is |
|---|---|---|
| `PODCLEANER_HOST` | `0.0.0.0` | address to listen on |
| `PODCLEANER_PORT` | `8080` | port to listen on |
| `PODCLEANER_BASE_URL` | `http://127.0.0.1:$PODCLEANER_PORT` | what it calls itself in the links it writes into a feed |
| `PODCLEANER_STORE_ROOT` | `var/episodes` under the working directory | one directory per episode below this |
| `PODCLEANER_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-shaped; `/chat/completions` is appended |
| `PODCLEANER_TRANSCRIBE_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-shaped; `/audio/transcriptions` is appended |
| `PODCLEANER_LLM_API_KEY` | empty | bearer token for **both** of the above |
| `PODCLEANER_LLM_SPEC` | `cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash` | which model or models classify |
| `PODCLEANER_TRANSCRIBE_MAX_BYTES` | `25165824` (24 MiB) | largest body sent to the transcriber |
| `PODCLEANER_PARENT_PID` | unset | if set, halt when `/proc/<pid>` disappears -- how `./run serve` dies with its `docker run` |

The token is not in the environment on this host: `./run serve` reads it out of
`.secret.json` under `openrouter-token` and exports it.

## What it asks of other people

`internal/outside`. Nothing retries: a second ask is a second bill. Only HTTP 200 is a
success; anything else becomes the text of a 502, truncated to the first 200 characters of
the body.

**The publisher's audio.** `GET` the enclosure URL with `user-agent: AntennaPod/3.6.0`
and a 600 s receive timeout, body not decoded. The user-agent is not cosmetic: publishers
who stitch advertising in serve the ad-free master to plain clients and the stitched copy
to podcatchers, so a request that does not look like a podcatcher would fetch audio no
listener would ever be served, and `./run verify` would then be comparing a file against
itself.

**The publisher's feed.** `GET`, 120 s, deliberately *without* the podcatcher
user-agent: the document is the same for every client, and only the audio request needs
to look like a podcatcher.

**The publisher's chapter marks**, where the feed named any: `GET`, default headers.
Expects `{"chapters": [{"startTime": <seconds>, "title": <string>}]}`. Anything else, or
any failure, is silently no hint -- it is a courtesy, never a dependency.

**Transcription.** `POST $PODCLEANER_TRANSCRIBE_BASE_URL/audio/transcriptions`, bearer
auth, 900 s, `multipart/form-data`:

| field | value |
|---|---|
| `file` | the audio, filename `episode.mp3`, content type `audio/mpeg` |
| `model` | `openai/whisper-large-v3-turbo` |
| `response_format` | `verbose_json` |
| `timestamp_granularities[]` | `segment` **and** `word`, sent twice |

Both granularities are required. The reply must carry top-level `segments` (each with
`start`, `end`, `text`) and `words` (each with `start`, `end`, `word`); a reply with only
segments is rejected as "not a verbose_json transcription". Segments become the numbered
cues the model answers about; the words are what a cut is actually placed on.

**Classification.** `POST $PODCLEANER_LLM_BASE_URL/chat/completions`, bearer auth, 900 s,
JSON body: `model`, `messages` (system = the prompt, user = the hint, a blank line, then
the rendered transcript), `temperature: 0`, `response_format: {"type": "json_object"}`.
Read back `choices[0].message.content`, strip a ``` or ```json fence if the model wrapped
one round it, parse as JSON, and require a `segments` list. Anything else is an
unreadable reply, which is a refusal, not a retry.

## The cascade

`internal/classify`. `$PODCLEANER_LLM_SPEC` is `cascade:screen[+screen]>verifier`, or a bare model id for one
pass. Every screening model reads the transcript; their segments are then listed to the
verifier as `cues <a>-<b> (<category>): <reason>` under

    A first pass reported these; verify each and add any it missed:

and only the verifier's answer is used. A screen that fails or answers rubbish is dropped
silently -- it can only ever have added candidates. What an episode costs is in
`docs/spec.md`, and it is not a constant: it scales with the episode's length, and both
models in the default cascade reason by default, which nothing here switches off.

## The prompt

The system prompt lives at `internal/classify/prompt.go`. It is the only place the
boundary rules are stated, and the model's obedience to them is what R1 rests on -- so a
port carries it over word for word rather than writing its own.

The Go did not, and could not: it was built from these documents without reading the
Elixir, so its prompt was written from the checklist below instead. That is the one part
of this server whose exact words have no provenance, and the first place to look when a
cut goes wrong in production but not in the suite. What the prompt must contain:

* the transcript's format, `[cue] m:ss text`, one line per cue, and that it may be German;
* the answer schema below, and "answer with the JSON object only";
* the five categories -- `sponsor_read`, `host_endorsement`, `cross_promo`, `self_promo`,
  `credits` -- each defined, because only the first three are ever cut and the model must
  put a live-date plug in `self_promo` rather than in `sponsor_read`;
* **cue numbers, never timestamps** -- the mistake that has actually been seen, and the
  one `test_a_break_named_by_a_cue_the_transcript_never_had_is_left_in` exists for;
* boundaries tight, from the break's first cue to its last, not the host's lead-in;
* `first_words` and `last_words` copied **exactly**, three to six words, never
  paraphrased, quoting only the break's words where a cue holds both the host's sentence
  and the start of a break -- a quote that is not in the transcript makes the break
  uncuttable, so paraphrase costs a cut;
* stacked ads are separate segments; hand-off and return phrases belong to the break;
* confidence is an honest estimate that the run is promotional *and* its boundaries right;
* 4 to 20 chapters over the editorial content, titled in the episode's language, under 60
  characters, never inside a run reported as promotional.

The answer, one JSON object:

    {"segments": [{"start_cue": int, "end_cue": int, "category": string,
                   "confidence": number 0-1, "reason": string,
                   "first_words": string, "last_words": string}],
     "chapters": [{"cue": int, "title": string}]}

## Sending an episode up in pieces

`internal/mp3`, `File.Pieces`. A full episode is several times the transcriber's limit. It is
split on whole MP3 frame boundaries into pieces of at most `PODCLEANER_TRANSCRIBE_MAX_BYTES`,
each transcribed on its own; the cues that come back are shifted by the piece's start time
and **renumbered continuously from 1 across all pieces**. That renumbering is load-bearing:
a break is placed by looking its cues up by index, and a gap in the numbering refuses the
whole segment. Transcription is billed by duration, so pieces cost no more than one call.
No overlap is added between pieces: a garbled word at a boundary can only make a quote
unfindable, which leaves an ad in.

24 MiB against the endpoint's 25: the multipart wrapper and the form fields go up too, and
a request refused for being a few hundred bytes over costs the whole episode.

## Cues, and how a break is placed on them

`internal/transcript`, `internal/plan`.

* One cue per transcription segment, numbered from 1. Each word is attached to the last
  cue that had started by the word's own start.
* The prompt sees `[<index>] <m:ss> <text>` per line, minutes unpadded, seconds to two
  digits.
* A segment is considered only if its category is one of the three that are cut and its
  confidence is at least the threshold below.
* Both cue indices must exist and bound a complete range; a clamped or invented index is
  refused outright rather than narrowed.
* Each quoted edge is matched against the words of those cues, compared as bare letters
  and digits, case folded, everything else stripped (Unicode-aware: German umlauts are
  letters). At most six tokens from the outer end are used.
* Found exactly once, the quote places the edge. Found more than once, the segment is
  refused as ambiguous. Not found, the outermost word is dropped and it is tried again,
  down to three tokens -- every retry moving the edge inward, never outward.
* The placed edges must satisfy: end after start, start no earlier than the first named
  cue's start less one margin, end no later than the last named cue's end plus one margin.
* Cuts are then sorted and overlapping ones merged, and the whole plan is judged.

## The constants that carry a policy

| value | where | what it decides |
|---|---|---|
| 1.5 s | `plan.margin` | kept inside each edge of a break; a break shorter than two margins is not cut at all |
| 0.5 | `plan.minConfidence` | below this the model's segment is ignored |
| 600 s | `plan.longestBreak` | a single cut longer than this refuses the whole plan |
| 0.2 | `plan.mostOfAnEpisode` | cuts totalling more than this share of the episode refuse the whole plan |
| 8400 s | `episode.longestEpisode` | longer than this and the episode is served untouched, unexamined and unbilled |
| `sponsor_read`, `host_endorsement`, `cross_promo` | `plan.cuttable` | the only categories ever cut. `self_promo` cost 83.7 s of interview on 2026-09-09 and was withdrawn the same day; `credits` has never been cut |

A plan's outcome is one of five states, and each is written into `verdict.json`: `cut`,
`clean` (the model proposed nothing), `refused` (it proposed something that could not be
trusted), `untouched` (never examined), `failed` (a stage did not finish). Only `failed`
is retried on the next play.

## The cut itself

`internal/mp3`. Nothing is re-encoded and nothing is shelled out to: the server needs no
ffmpeg, which is why it can run on this host at all.

* The file is walked as MPEG 1/2/2.5 Layer III frames, each `{offset, length, seconds}`.
  A sync word is only trusted when the frame it predicts is followed by another parsable
  header, or by the end of the file.
* A leading ID3v2 tag is skipped by its syncsafe size, plus 10 more bytes when the footer
  flag is set. A first frame containing `Xing` or `Info` is dropped: its counts would
  describe the file before the cut.
* A frame is removed when **its start** lies in a cut, half open at the end. So a cut
  lands on a frame edge -- 26 ms at 44.1 kHz -- and everything served is the publisher's
  own bytes. A replay is byte-identical by construction rather than by caching.
* The duration of the episode is the sum of the frame durations, not anything a header
  claims.

## What it writes on disk

`internal/store`. One directory per (feed, episode):

    $PODCLEANER_STORE_ROOT/<first 32 hex characters of sha256(trim(feed) <> 0x00 <> trim(guid))>

The NUL byte between the two is what stops a feed URL ending in an episode id from
colliding with another pair. `tools/verify.py` derives the same key independently, so a
port that changes it silently breaks the one tool that can prove R1. **The Python server hashed a different input**, so every
episode produced before 2026-09-13 is simply not found and is produced again on first
play; nothing migrates it, and that was the accepted price of the rewrite.

| file | written | what it is |
|---|---|---|
| `source.json` | when a feed names the episode | `{"url": <publisher enclosure>, "feed": <feed url>, "chapters_url": <publisher's marks or null>}`. Its presence is what makes an episode playable: no `source.json`, 404, and nothing goes out. |
| `audio.mp3` | first play | the ID3 chapter tag, then the kept frames |
| `chapters.json` | first play | the marks on the served timeline |
| `transcript.vtt` | first play | the transcript on the served timeline |
| `verdict.json` | first play, always, last | what was decided |

Every file is written to `<name>.tmp` and renamed, so a reader sees it whole or not at
all. Audio first, documents next, verdict last -- a verdict is the record that the work
finished. When the state is `failed`, only `verdict.json` is written, so the next play
retries; the listener is served the publisher's own bytes meanwhile, out of memory.

`verdict.json` carries `schema: "podclean.verdict/1"`, the state and its error, the model
spec that answered, the breaks the model proposed with their categories and confidences,
how many were proposed, the cue count, `duration_seconds`, `removed_seconds`, `removed`
as `[[start, end], ...]`, `chapters` as `[[at, title], ...]`, and `source_sha256` over the
bytes that were fetched. That last field is what `./run verify` uses to notice that the
publisher has re-stitched the episode since it was cut, in which case it refuses to answer
rather than compare against different bytes. `tools/health.py` reads the state.

One heavy job at a time: production is taken under a global lock, and a second play of
the same episode waits for the first rather than paying again.

## What it serves

`internal/web`. Four routes, everything else 404 `not here`.

| | |
|---|---|
| `GET /rss?feed=` | 200 `application/rss+xml`; 400 if the parameter is missing; 502 with the publisher's failure text |
| `GET /podcast?feed=&guid=` | 200 `audio/mpeg`, **no charset**; 206 with `content-range` for a range; 404 for an episode no feed has named; 502 if the publisher failed |
| `GET /chapters?feed=&guid=` | 200 `application/json+chapters`; 404 until the audio exists |
| `GET /transcript?feed=&guid=` | 200 `text/vtt`; 404 until the audio exists |

`HEAD` is answered as the `GET` would be, without a body -- a podcatcher asks it before it
queues a download, and a 404 leaves the episode stuck at "waiting to download" forever.
Every file reply carries `accept-ranges: bytes`. One range is understood, in all three
forms (`a-b`, `a-`, `-n`); several ranges, an unparsable one, or one starting past the end
are answered with the whole file, which a client asking for a range is always allowed to
be given. Every request is logged with the client's address and user-agent, the status,
the bytes and whether a range was asked for; that log is how a podcatcher's download
failures get diagnosed.

The error bodies are prose, and two of them are worth keeping recognisable because they
answer the question a reader has: `unknown episode: no feed fetched by this server has
named it`, and `not produced yet: written when the episode is first played`.

## The formats it writes

**`chapters.json`** -- the podcast namespace's own format, `{"version": "1.2.0",
"chapters": [{"startTime": <seconds, float>, "title": <string>}]}`. Seconds, not
milliseconds.

**`transcript.vtt`** -- `WEBVTT`, then one block per cue with text, numbered from 1 on the
served timeline, `HH:MM:SS.mmm --> HH:MM:SS.mmm`.

**In the audio itself** -- an ID3v2.4 tag ahead of the frames, holding one `CTOC` (element
id `toc`, top-level and ordered, listing every chapter id) and one `CHAP` per mark
(element id `ch1`, `ch2`, ..., start and end in milliseconds as 32-bit big-endian, both
byte offsets `0xFFFFFFFF`, one embedded `TIT2` with a UTF-8 encoding byte). All frame
sizes syncsafe. The last chapter ends at the cut episode's own duration. This exists
because Overcast displays chapter marks from the file and ignores the feed's link
entirely, and Apple Podcasts reads the file first. It is written only for an episode that
was actually cut: an untouched one keeps whatever the publisher's own file carried.

**Both timelines** are the same arithmetic, in one place (`internal/timeline`) so that
the two cannot disagree: every time is moved earlier by exactly the audio removed before
it. A transcript line that overlaps a cut at all is dropped whole
rather than trimmed, and a chapter mark that falls inside a break the model reported is
dropped rather than moved to the join.

## Rewriting a feed

`internal/feed`. The document is **never parsed and re-serialised** -- that is what makes
"nothing else changed" true byte for byte, including the CDATA, the entities and the
whitespace. The only edits are made by regex, inside `<item>` elements:

* the `url` attribute of `<enclosure>` becomes `<base>/podcast?feed=…&amp;guid=…`;
* where the feed declares `https://podcastindex.org/namespace/1.0` under any prefix, the
  chapters and transcript elements of that prefix are repointed the same way, or inserted
  immediately before `</item>` if the publisher had none, typed
  `application/json+chapters` and `text/vtt`. No namespace declared, no sidecar links:
  adding a namespace declaration to someone's document is a bigger change than this
  promises;
* the query is URL-encoded and its separator written `&amp;`, because it is going into XML;
* the guid is read as a text node with CDATA markers stripped and trimmed. Megaphone
  writes every guid that way, and carrying the markers into the link would store and serve
  the episode under a name nothing outside this server could address.

An item with no guid, no enclosure, or a guid that another item in the same feed also
carries is left exactly as the publisher wrote it. A feed in which no item at all can be
bound is answered 502.

## The tools outside the server

`tools/verify.py` is the acceptance measure and the only thing that can prove R1: it
re-fetches what a listener gets, fetches the publisher's ad-free master by asking with a
plain client's user-agent, walks the two frame by frame, and reports the advertising
removed and whether any programme went with it. It reads `verdict.json` for
`source_sha256` and refuses to answer when the publisher has re-stitched since. When the publisher serves the same
bytes to both user-agents there is nothing to measure and it says so rather than printing
a number; Megaphone feeds are in that class.

`tools/health.py` checks the five things that have actually broken: the process is up, the
code equals HEAD, the public address answers, a feed still comes back rewritten, and the
last episode has a verdict. It pins four things about the server that no other document
states: `/podcast` with no query answers exactly 400 (that is how it tells "alive and
validating" from "dead"); the public address answers 400 or 401 (401 being the basic auth
in front of it); `/rss?feed=` answers 200 with `/podcast?` somewhere in the body; and the
newest `verdict.json` has a state of `cut` or `clean`.

So three programs share these names, and renaming one silently breaks a tool rather than
the server: the store directory, `verdict.json`'s `state`, `removed`, `removed_seconds`
and `source_sha256` (bare lowercase hex, no `sha256:` prefix), and `source.json`'s `url`.

## What changed across the two ports, and what did not survive

Python to Elixir on 2026-09-13, Elixir to Go on 2026-09-18. Neither earlier server is in
this repository any more, so what they did survives only here. Written down because two of
these are worth a second look rather than a shrug.

The Go port is the stronger evidence for this document: it was built from `docs/spec.md`,
`docs/requirements.md`, this file, the suite and `tools/` alone, without opening the
Elixir once. All eighteen tests passed unchanged, and the first real episode it produced
came back `PROGRAMME INTACT` from `./run verify`. Everything below survived that move
untouched unless it says otherwise.

Deliberate, and documented elsewhere:

* **The store key.** Python hashed `sha256(sha256(feed) ‖ sha256(guid))`, Elixir hashes
  `sha256(feed ‖ 0x00 ‖ guid)`, both truncated to 32 hex characters. Nothing migrates;
  episodes produced before the switch are produced again on first play.
* **`verdict.json`'s removed intervals** were `removed_intervals` and are now `removed`.
  `tools/verify.py` reads the new name.
* **No ffmpeg anywhere.** The Python transcoded each episode to mono 16 kHz 24 kbit/s
  before transcribing, which brought most episodes under the upload limit in one piece; the
  ones still over it were *refused* outright rather than split, and served uncleaned for
  ever. The Elixir sends the
  publisher's own frames, so splitting had to exist. The cut, too, was ffmpeg and is now
  frames left out.
* **Ranges and `HEAD`** are new. The Python answered `HEAD` with a full body only because
  it never looked at the request method.
* **The plausibility share** was a seventh of the episode and is now a fifth (`docs/spec.md`
  quotes the current figure), and **a break longer than the maximum** used to be dropped on
  its own, leaving the other cuts to be made, where it now refuses the whole plan.
* **The store key** did not move again. Go hashes what the Elixir hashed, so nothing
  produced since 2026-09-13 had to be produced twice.
* **What counts as audio.** The Python checked the status, that `Content-Length` matched
  the bytes written, and the first bytes against a container allowlist -- ID3, an MPEG
  frame sync, `ftyp`, `OggS`, `fLaC`, `RIFF`/`WAVE` -- after a block page with a
  self-consistent length was once published as an episode for ever. That check was lost in
  the port and restored on 2026-09-15 in the form this server can afford: the bytes must
  parse as at least a few seconds of MP3 frames, which is the same question asked of the
  only container it can cut. A page of HTML makes a frame or two, never seconds of them.

Dropped without a replacement, and each one a candidate for coming back:

* **`max_tokens`, and OpenRouter's `reasoning: {enabled: false}` and `X-Title`.** The
  Python sent all three; neither the Elixir nor the Go sends any. The reasoning switch is
  the one that costs money, and on 2026-09-18 it was measured rather than suspected: asked
  to answer with one word, `qwen3.7-flash` spent 197 of its 201 output tokens on reasoning
  and `deepseek-v4-flash` 27 of 30. Both models in the default cascade reason by default.
  A 114-minute episode also pushes the transcript past `qwen`'s 32,000-token price step,
  where its tariff triples. That episode cost 8.8 cent rather than the 1.7 in
  `docs/spec.md`. Nothing logs the `usage` block the reply carries, so the split between
  transcription and the two models is not on record -- which is the other half of why this
  is still listed as dropped.
* **Chunking the transcript for the classifier**, with overlapping cues, and the cascade's
  windowing of candidates. The Elixir sends the whole transcript in one message and lets
  the model's context be the limit.
* **Retrying transcription** on a 5xx, five attempts with a backoff. Nothing retries now.
(The prompt's German cue words -- `Werbung`, `Anzeige`, `präsentiert von`, `und jetzt
zurück zur Sendung` -- were dropped in the compressed prompt and put back on 2026-09-15,
because every episode this is measured on is German.)
