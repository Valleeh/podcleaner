# PodClean — what it does, from outside

Everything below is observable without reading the source: make a request, look at what
comes back, listen to it, or compare it against what the publisher serves. Where a claim
has a number, the number was measured and the command that reproduces it is named.

## What it is

A podcatcher subscribes to a feed served from here instead of from the publisher. The
episodes it downloads are the publisher's, with the advertising cut out.

Nothing else about the subscription changes: same show, same episode list, same titles
and show notes, same publication dates.

A chapter list and transcript may be added.

## The promise, and its price

**No second of programme is ever removed.** Advertising that survives is an annoyance;
a sentence that disappears is not recoverable, and the listener cannot even tell it is
gone. Every choice here resolves that way: where it is not certain that a second is
advertising, it stays.

The price is paid in the other direction, and it is not small: **on the episodes
measured, between a third and a half of the advertising survives.** That is the deal.

## The URLs

### `GET /rss?feed=<publisher feed url>`

Returns the publisher's feed document with **three things changed, all of them links**:
every episode's audio link, its chapter-mark link, and a transcript link this server adds
where the publisher had none. All three now point here. Byte for byte, nothing else
differs — not the titles, not the CDATA show notes, not the itunes tags, not the feed's
own address, not the character encoding. You can check this: fetch both, put the original
audio and chapter links back into ours, drop the transcript links ours added, and the two
documents are identical.

The chapter link is repointed rather than left alone because the publisher's marks are
timed against the publisher's audio, and this server serves shorter audio. Leaving it
would put every mark minutes out — which is the failure this document used to avoid by
serving no marks at all. A feed that does not declare the podcast namespace gets neither
link and keeps exactly what it had: adding a namespace to someone's document is a bigger
change than this promises.

Two episodes in one feed that share an identifier are both left pointing at the
publisher, because a link that resolves to the wrong episode is worse than no link.

### `GET /podcast?feed=<publisher feed url>&guid=<episode id>`

Returns the episode's audio.

* **The first request takes about three to five minutes** and costs a few cents. The
  connection stays open the whole time; no partial file is ever sent. The cost is not a
  constant: it rises with the episode's length, and most of it is the reading, not the
  deciding. A 73-minute episode was billed at 1.7 cent; a 114-minute one, measured on
  2026-09-15, at 8.8.
* **Every request after that is instant** and returns byte-identical audio. The episode
  is produced once, never re-examined, and never re-billed.
* A pair no feed fetched by this server has ever named is **404**, and no request goes
  out to anyone.
* If the publisher's server fails, this one answers **502** and keeps nothing. A
  publisher that answers a download with something that is not audio -- a block page
  carrying an episode's content type and its own length -- is such a failure, and is not
  published as the episode.

### `GET /chapters?feed=<publisher feed url>&guid=<episode id>`

The episode's chapter marks, in the format the publisher's own marks use, timed against
the audio this server serves.

The marks are not the publisher's. They are written in **the same pass that finds the
advertising** -- that pass reads the whole episode either way, so the marks cost a few
hundred words of its answer and no second reading, which is why the figure above did not
move. The publisher's own marks are handed to it as a starting point where the feed has
any. Every mark is then moved by exactly the audio removed before it, so it lands on the
sentence it named in the original.

Because one pass decides both, the marks know where the advertising is: a chapter the
model put inside a break it also reported is dropped rather than moved to the join.

A cut episode carries the same marks inside the audio file as well, in the form a
podcatcher reads from an MP3 itself: Overcast displays those and nothing else, and Apple
Podcasts reads the file first. One list, written twice, so the two cannot disagree. An
episode served untouched keeps the publisher's own marks, which are right for audio
nothing was removed from.

### `GET /transcript?feed=<publisher feed url>&guid=<episode id>`

The episode's transcript as WebVTT, timed against the audio this server serves. It is the
transcript the advertising was found from, with every line that overlapped removed audio
dropped and the rest moved by exactly the audio removed before it.

Both of these answer **404** until the episode has been played once. They describe audio
that is produced on the first play, and there is nothing to describe before it exists.
Neither ever starts that work, and neither costs anything once it is done: both are read
from disk.

## What you get when you play it

* The audio is the publisher's own, minus whole advertising breaks.
* **Cuts land inside the advertising, and you hear 1.5 s of every break at each end.**
  A cut begins 1.5 s after the break's first word and ends 1.5 s before its last. The
  join may fall in the middle of a word: what is on both sides of it is advertising.
  The margin is deliberate. It is where an error of a word at either edge lands, and a
  word of advertising is an annoyance where a word of programme is a loss.
* **The file is shorter** by exactly what was removed, to within one MP3 frame (26 ms):
  the cut leaves whole frames out and re-encodes nothing, so every byte served is the
  publisher's own. A replay is byte-identical because the same frames are left out.
* The episode carries **no cover art**, even where the publisher's did.

## What it does not do

* **Self-Promo is left in** — the show's own live dates, its
  own other podcast, its own outro. Cutting that category once removed 83.7 s of
  interview from an episode, and it was withdrawn the same day.
* **around 1 - 3 s of advertising survives at each edge** of every break that is cut, by the
  margin above. A break is found to the word, not to the transcript's line: a line that
  holds the host's last sentence and the break's first words is cut from the break's
  words on. Cutting from the line's start once removed 5.2 s of programme.
* **A break the model is not sure about is left in**: one reported below 0.5 confidence
  is ignored. So is a plan that is not believable as a whole: one break longer than
  10 minutes, or breaks adding up to more than a fifth of the episode, and the episode is
  then served untouched, all of it.
* **Whole breaks are sometimes missed.**
* **A dropped transcript line leaves a hole.** A line that overlapped a cut is dropped
  whole rather than trimmed, so a few seconds either side of each join have audio and no
  text. Trimming it instead would leave a line claiming words that are not in the file.
* **Chapter titles are the model's own** and can be wrong about what a passage is called.
  Only the titles are a judgement; the times are arithmetic on what was removed.
* **A chapter mark that fell inside advertising is dropped**, rather than moved to where
  that break was cut out. So the stretch just after a cut carries the title of the chapter
  before it — a title written for audio that is still there — where moving the mark would
  have stood an advertiser's name over the programme.
* An episode longer than about two hours twenty is served untouched.
* Once an episode has been produced it is never revisited, so an improvement made later
  does not reach episodes already fetched.

## Measured

Lage der Nation, verified against the publisher's own ad-free master:

| episode | date | advertising in the file | removed | programme lost |
|---|---|---|---|---|
| LdN492 | 2026-09-15 | 191.7 s | **91.0 s (47.5%)** | none |
| LdN490 | 2026-09-11 | 285.6 s | **157.2 s (55.1%)** | none |
| LdN492 | 2026-09-10 | 285.6 s | **178.0 s (62.3%)** | none |

The 2026-09-15 row is the current server's first measured episode. One of its three breaks
came out at nothing: the reading quoted a single word as that break's last words, and one
word cannot place a cut edge, so the whole 62 s of it still plays. That is the promise
working rather than failing, and it is most of the difference between that row and the one
above it. The 2026-09-11 row is the first with cuts placed by the break's own words and
the 1.5 s margins; the 2026-09-10 row is from the pause-based cuts that preceded them and
has not been re-run. The advertising total is a property of the copy that was fetched, not of the
episode: a publisher who stitches ads in builds a fresh copy per request, and earlier
fetches of LdN490 carried 291.6 s and 266.0 s where this one carries 285.6 s. So the
seconds move between runs of the same command on the same episode. What does not move is
the last column.

The break at the very end is left whole in both episodes: the classifier did not report
it at all, so that is a missed break, not an uncuttable one, and it is 66 s in each. The
third break of LdN490 was refused on 2026-09-11: its first line is the single word
"Werbung.", the transcriber stamped that word 29.6 s late, and a cut that cannot be
anchored to the break's words is not made.

Reproduce either:

    ./run verify <feed url> <episode guid>

It re-fetches what the listener would get, compares it against the publisher's ad-free
master frame by frame, and prints the advertising removed and whether any programme went
with it. If the publisher has re-stitched the episode since it was cut, it says so and
refuses to answer — a number computed against different bytes would mean nothing.

## Running it

    ./run build                     build the image it runs in
    ./run serve                     start it, in that image, on the host's network
    ./run health [feed url]         is the deployed thing working
    ./run verify <feed> <guid>      was a cut right
    ./run test                      the suite (needs Docker)

`./run serve` runs it with the store mounted at its own path and the publisher-facing
credentials taken from the secrets file; `./run test` starts it the same way. It needs no
media tools of its own — it removes advertising by leaving the publisher's own encoded
audio out, not by re-encoding what is left.

`./run health` checks the five things that have actually gone wrong: the process is up,
it is running the code that is checked out, the public address answers, a feed still
comes back rewritten, and the last episode has a result. Every outage so far was one of
these, and none was a bad cut.

Subscribing means pasting the `/rss` URL into a podcatcher, with credentials if the
address is protected. Episodes already downloaded under the publisher's own feed keep
playing from there — a podcatcher remembers where it first saw an episode, so only what
appears after the switch comes through here.

Each produced episode stays on disk at roughly the size of the original. Nothing deletes
them.

To make an episode be produced again — after an improvement, or because a cut was
wrong — delete the audio file that was published for it; the next play makes a new one.

# Future thoughts
* **What is removed lies inside advertising.** Verifiable, and verified: publishers who
  stitch advertising in serve the ad-free master to a plain client, so the two can be
  walked frame by frame against each other. That comparison shares nothing with the part
  that does the cutting.