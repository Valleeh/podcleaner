# MVP phase

Replaces `next-steps.md`. This is the only list of what happens next. When a step is done,
tick it here; when the whole list is done, this file goes away.

## What an MVP phase is here

**One sentence has to become true.** For PodClean:

> I subscribe to the feed in my normal podcatcher, press play, and hear no advertising.

That sentence is the whole specification. A thing belongs in this phase if the sentence is
false without it, and does not belong if the sentence is still true without it. There is no
other admission test, and the question is worth asking out loud before writing any file.

**Done means** one real episode, in a real podcatcher, with nobody touching anything.
Not a passing test suite. Not a demo script. The podcatcher or nothing.

**Explicitly not in this phase:** speed, cost optimisation, more than one listener,
surviving a crash halfway through a job, coverage numbers, configuration frameworks, and
anything whose justification starts with "later we will want".

**One rule still applies in full.** Never remove real content; when a cut edge is
uncertain, refuse to cut. This is not a quality bar that gets raised after the MVP, because
a bad cut is unrecoverable for the listener. Everything else can be crude.

## What we are deliberately not building

| Dropped | Why it can wait |
|---|---|
| Queue, worker, leases, fencing tokens (`old/podcleaner/core/`, 1261 lines) | They exist to survive a crash mid-job and to run several workers. The MVP is one process serving one listener. If it dies, the next request redoes the work — `architecture.md` says as much itself. Atomic rename already gives the durability that matters: a file that exists is complete. |
| Fingerprint tier (`old/podcleaner/detect/fingerprint.py`, 1239 lines) | It saves money on repeat ad creatives. The cost target is already beaten fourfold, so it buys nothing the sentence needs. It is also the module whose zero-false-positive claim cites a test file that has never existed in any branch, and whose learn step adopts every model call unverified — one wrong classification would then repeat silently on every later episode. Adding it now spends risk against the one rule for a saving nobody is waiting for. |
| An explicit state machine | The filesystem is the state. The clean file exists, so serve it; it does not, so produce it. |
| A configuration layer | Module-level constants, overridable by environment variable, read in one place. |

## The shortcut that shapes the order

For Lage der Nation, fetching the episode with a plain tool User-Agent returns the ad-free
master, back catalogue included. For that feed the sentence becomes true with **no
transcription, no LLM and no cutting at all**. So the skeleton comes first and detection
comes after, for the feeds where the shortcut does not work.

The shortcut is a tier, never a replacement: try the clean master, verify it really differs,
otherwise fall back to the pipeline. The publisher can withdraw it any day and nothing may
break when they do.

## Steps

Each step ends with the `janitor` agent: delete what the step made unnecessary, check the
docs still match, run the tests, commit. Do not start the next step before that.

### 1. Close the guard — `mvp-build`, reviewed by `cut-guard`

The `degraded` flag is set when a model reply stays corrupted after the retry, but no cut
decision reads it, and `is_cut` is static so it cannot. Two defaults lean the same way: an
unknown category becomes `sponsor_read`, and merging overlapping segments keeps only the
first category.

Make `degraded` block cutting, drop unknown categories instead of guessing one, and keep
the more cautious category on merge.

*Done when* the corrupted-reply test asserts that nothing cuttable survives, and `./run test`
is green.

### 2. The skeleton: feed in, audio out — `mvp-build`

No detection in this step at all.

- `podcleaner/fetch.py` — download an episode. Try the plain User-Agent first, keep that
  result only if it genuinely differs from the podcatcher variant, else fall back.
- `podcleaner/feed.py` — fetch the origin RSS and patch `enclosure@url` in place. Do not
  rebuild the document; v1 did and lost the itunes tags, images and categories.
- `podcleaner/store.py` — episode path from the feed GUID, not from `md5(mp3_url)`; many
  CDNs vary the URL per request and the cache would miss every time. Per-episode lock,
  publish by atomic rename.
- `podcleaner/server.py` — `/rss?feed=` and `/podcast?url=`, the second one blocking.

*Done when* an LdN episode plays ad-free in a real podcatcher and no detection code ran.

### 3. Cutting — `mvp-build`, reviewed by `cut-guard`

Port `old/podcleaner/detect/boundaries.py` and delete everything the MVP path does not
call. Bring `old/tests/rebuild/test_boundaries.py` with it. Then `podcleaner/cut.py`: cut
with ffmpeg on snapped edges, and refuse when no clean edge is within tolerance.

*Done when* a known ad is cut out of a pinned fixture and both edges sound clean.

### 4. Wire detection into the path — `mvp-build`, reviewed by `cut-guard`

`podcleaner/analyze.py`: transcribe, classify, snap, cut. Reached from `server.py` only
when step 2's fetch found no clean master.

*Done when* one Solved episode goes from feed to cut audio end to end.

### 5. Prove the gates bite — `cut-guard` writes the mutations, `mvp-build` wires them

Perturb a cached prediction — shift an edge by ten seconds, drop a segment, add a five
second cut inside editorial — and assert the matching gate fails. Nothing yet proves the
assembled gates catch anything; the scorer's unit tests only cover the semantics.

*Done when* each mutation turns a gate red.

## Alongside, not blocking the MVP

Two things run next to the steps because they take wall-clock time nobody has to spend
watching. Neither makes the sentence true, so neither gates a step.

**Whole-episode transcription**, one at a time, overnight, nothing else on the box:
`./run full`. About two and a half hours for LdN491 and six and a half for Solved. It
unlocks the pure-whisper detection cases and full-episode WER baselines.

**A second opinion on four scoring refinements.** During the cheap-model search the
evaluation itself was changed four times: continuation cues at splice points became
don't-care, a stacked break reported as two segments counts as found, pauses up to three
seconds between a framing line and its ad are tolerated, and two segments were marked
ambiguous. Sonnet passed before all four; the cheap cascades needed them. Each is argued in
`integration-tests.md` section 6, but moving the goalposts and then reporting a pass
deserves a look from someone who did not move them.

## Alongside, human only

Nobody but the owner can do this, and it blocks re-scoring but nothing else.

    ./run labels checklist tests/integration/labels/ldn491.podcatcher.label.json
    ./run labels verify --label <file> --labeler <you> --ad 3
    ./run labels finish --label <file> --labeler <you>

| Episode | Ads | Open | Work |
|---|---|---|---|
| LdN 490 | 3 | 0 | one pass over the file, then `finish` |
| LdN 491 | 5 | 1 | a policy decision, no listening |
| Solved | 20 | 17 | real listening, four hours of audio |
| Hacks on Tap | 16 | 16 | listening, and it has no splice oracle |

The label builder regenerates these files, so a changed verdict goes into
`labels/sources/` and the builder must keep the `verified` flags rather than clobber them.
When the labels are finished, re-record the baselines without `--provisional-labels`.

## Open decisions for the owner

- **Which ambiguous segments count as advertising** for PodClean's policy: host
  recommendations of other shows, network cross-promos, opening announcements, closing
  credits. The labels carry them as don't-care until this is settled, and the cheap
  cascade's pass verdict may move with it.
- **Is the cheap cascade the product default, or a tier below Sonnet?** Both pass today's
  gates; only the cascade meets the cost target.

## After the MVP

Parked here so they are not lost, deliberately not scheduled. The fingerprint tier and the
queue from the table above; a fallback model spec for when a provider drops a model or rate
limits us (`deepseek>deepseek` at 0.40 cent passes the same gates); a wider corpus with
host-read ads that are not server-inserted, a second ad-free negative control and another
publisher serving a clean master; authentication, without which anyone reaching the box can
drive LLM spend.

## Known drift in `architecture.md`

Fix or delete when touching those sections. The whisper factor in section 2 and the runtime
view says 5.6x realtime and section 11 derives eight hours per episode from it; measured is
1.6x the audio length, so two and a half hours. The two "known defects carried from the
working version" in section 11 are not in `old/`: the serialisation call returns a string
correctly, and no whitelist exists anywhere in the tree.
