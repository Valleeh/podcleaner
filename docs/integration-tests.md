# Integration tests for the detection slice

Status: written 2026-09-06 against pinned episodes of *Lage der Nation* (German),
*SOLVED with Mark Manson* (English) and *Hacks on Tap* (English). Measured numbers are in
`tests/integration/baselines.json` and `var/reports/`; this document explains what is
measured, against what, and how much to trust it.

## 1. Two separate questions

The pipeline has two stages whose failures look alike from the outside (an ad survives, or
content is cut) but have different causes. The suite keeps them apart:

| Suite | Input | Reference | What a failure means |
|---|---|---|---|
| `test_transcription.py` | audio windows | publisher transcripts, labels, the cached whole-episode transcript, synthetic silence | whisper.cpp (or its invocation) regressed |
| `test_ad_detection.py` | **stored** transcripts | labels (constructed and text-derived) | the classifier, its prompt, the model or the policy regressed |

The ad tests never wait for whisper on a whole episode. They consume either the cached
whole-episode transcript (Hacks on Tap), or a *hybrid* transcript: the publisher's
transcript of the clean master mapped into the listener file's timeline, plus whisper
transcripts of exactly the inserted regions. `--full` adds the all-whisper variants once
the whole-episode transcripts exist.

## 2. Where the ground truth comes from

Real episodes have no ground truth by default; the repository's rule is that nothing may be
scored against labels nobody actually made. The suite gets its truth from four sources, in
decreasing order of trust, and every label carries which one it came from.

### 2.1 Constructed: server-side ad insertion, reversed

All three podcasts insert advertising server-side. Two of them serve the **clean master**
to a plain HTTP client and an **ad-stitched variation** to podcatcher User-Agents:

| Episode | clean (curl) | podcatcher UA | inserted |
|---|---|---|---|
| LdN490 | 79:49.0 | 82:13.2 | 3 regions, 144.11 s |
| LdN491 | 91:01.6 | 94:03.8 | 4 regions, 182.16 s |
| Solved "Finding Your Life Path" | 4:10:20.6 | 4:13:14.9 | 3 regions, 174.34 s |

The stitched files contain the clean file's MP3 frames **byte for byte** plus inserted
frames. `podcleaner/eval/dai.py` walks both frame sequences in lockstep and returns the
inserted regions exact to one frame (26 ms). The result checks itself: inserted seconds
must equal the duration difference of the two files, which they do to the millisecond for
all three episodes (`tests/integration/dai/*.json`). Nobody listened, nobody judged.

Two things this revealed: LdN's inserted regions are two 38-40 s sponsor spots (Vattenfall,
Versicherungskammer Bayern) and a 66 s **house promo** for the live tour; and the ads
inserted into the English Solved episode are **German** (Shopify, EY), geo-targeted at the
downloading client. Whisper, forced to English, translates them.

One caveat surfaced while cross-checking: `ffprobe` reports the Solved files 22.5 s longer
than they are, because the publisher's encoder wrote a wrong Xing header and ffprobe trusts
it. Decoding the audio (`ffmpeg -f null`) agrees with the frame walk to 0.1 s, so the manifest
records decoded durations and keeps the header value separately. A duration oracle is only
independent if it measures the audio rather than a header.

Hacks on Tap (Megaphone) changes the ad load on every request and offers no clean master,
so its labelled file is the one downloaded on 2026-08-31 and cannot be re-fetched.

### 2.2 Publisher transcripts (silver references)

Both LdN and Solved publish `<podcast:transcript>` VTT files. LdN's is a per-speaker-track
ASR of the clean master (`<v Track n>`, sub-second timestamps); Solved's is Flightcast's
caption ASR. Neither is human-proofread (LdN's writes "Ulf Bohmeier"), so WER against them
mixes whisper's errors with theirs. They are stable and repeatable, which is what a
regression gate needs, and far better than YouTube auto-captions. Both cover only the clean
master, which is exactly why 2.1 works.

### 2.3 Text-derived labels (need a human)

Solved's seven host-read sponsor segments are baked into the master. Their cue ranges were
located by reading the publisher transcript (`labels/sources/solved-life-path.text-ads.json`)
and mapped into the podcatcher file's timeline through the DAI offset map. Hand-off and
return phrases ("a word from our sponsors", "welcome back") are labelled **ambiguous**, so
whether a model includes them changes nothing. The Hacks on Tap annotation was made the same
way by the repository owner in the `whisper-transcribe` repo and is imported unchanged, with
the music stings at ad edges split out as ambiguous.

These segments are candidates until a human listens to their boundaries. The label file
stays `in_progress`, the checklist (`labels/*.VERIFY.md`) says what to listen to, and
`python -m podcleaner.eval.labels finish` refuses until every text-derived segment is
marked verified. The ad tests skip `in_progress` files unless `--provisional-labels` is
passed, and then report the label status alongside the numbers.

### 2.4 Negative controls

A detector that says yes to everything passes a positive-only suite. Controls here:

- LdN490's clean master contains no advertising (publisher transcript and durations agree).
  Its whisper transcript goes through the classifier; **any** cut fails.
- 30 s of digital silence and 30 s of low white noise go through whisper; more than two
  invented words fails. Whisper is known to hallucinate on such input, and whatever it
  invents would be classified downstream.
- Garbage from the model (indices outside the rendered cues, a "segment" spanning the whole
  episode) is exercised offline in `tests/unit/test_llm.py`; it must be dropped or left
  uncut, never clamped into a cut.

## 3. What is asserted

### Transcription (per 120 s window, three per episode with a reference)

| Check | Gate |
|---|---|
| WER vs publisher transcript | absolute ceiling 30 % (de) / 25 % (en); regression: baseline + 3 points |
| Language detection | must equal the feed's language |
| Timing: first 4 words of each reference cue located in whisper's output | ≥ 50 % found; p90 offset ≤ 1.5 s; regression: baseline + 0.3 s |
| Coverage / loops | speech coverage within 0.2 of the reference's; no cue > 30 s; no line repeated 3× |
| Ad anchor lines (windows over labelled ads) | first and last line of every ad found (similarity ≥ 0.6) within 3 s of the label |
| Drift (Hacks on Tap editorial window) | WER vs cached whole-episode transcript ≤ 10 % |
| Silence / noise | ≤ 2 words |

Windows are used because whisper small runs at about 0.65x real time on this host: the
default run transcribes ~23 minutes of audio (~35-40 min wall clock, cached afterwards);
`--full` adds whole episodes (hours) and produces the transcripts the all-whisper ad cases need.

### Ad detection (per episode, policy `promos`, confidence ≥ 0.5)

| Check | Gate |
|---|---|
| Content safety | 0 s of editorial cut more than 3 s from any labelled ad edge (hard) |
| Boundary slop | ≤ 6 s per labelled segment in total |
| Recall | every labelled in-policy segment found (≥ 50 % covered); ≥ 90 % of the ad seconds that lie under a transcript cue are cut (the jingle after an ad's last word is not cue-covered and cannot be cut by a cue-aligned classifier; raw recall is reported too) |
| Negative control | LdN490 clean: no cuts |
| Regression | combined asymmetric score (missed + 3 × false cut) ≤ baseline + 5 s |
| Self-check | constructed regions reconcile with file durations and with the label files |

The 3 s edge tolerance reflects cue granularity: both the labels and the classifier work on
cue edges, which sit up to a few seconds from the true acoustic edge. Snapping to silence
and the refusal rule (arc42 ADR-8) are downstream of this suite.

## 4. Cost and caching

- Whisper results are cached under `var/cache/whisper/` by content hash and parameters.
- LLM replies are cached under `var/cache/llm/` by prompt hash (`--llm-fresh` bypasses).
  A cached run therefore measures a *stored* model reply; re-run fresh before quoting a number.
- One full pass of the ad tests costs about 1.2 cent with the default cascade, or roughly
  0.7 USD with Sonnet (`--llm-model anthropic/claude-sonnet-5`).

## 5. Measured results (2026-09-06, Claude Sonnet 5 via OpenRouter, reasoning disabled; for the cheap default see section 6)

Ad detection, policy `promos`, confidence ≥ 0.5, edge tolerance 3 s, labels still
provisional (no human has listened yet). "Recall (cued)" is recall over the ad seconds that
lie under a transcript cue.

| Case | Transcript | Gold segments | Found | Recall | Recall (cued) | Content cut > 3 s from an edge | Boundary slop | Calls / prompt tokens |
|---|---|---|---|---|---|---|---|---|
| LdN491 | hybrid | 3 inserted spots | 3/3 | 86.2 % | 100 % | 0 s | 0 s | 1 / 63k |
| LdN490 | hybrid | 2 inserted spots | 2/2 | 88.5 % | 100 % | 0 s | 0 s | 2 / 119k (one retry after a corrupted reply) |
| Solved | hybrid | 7 host reads + 3 inserts | 10/10 | 99.1 % | 99.4 % | 0 s | 1.8 s | 3 / 132k |
| Hacks on Tap | whisper | 8 (3 pre-roll promos, 2 reads, 3 promos) | 8/8 | 97.2 % | 97.2 % | 0 s | 0 s | 1 / 36k |
| LdN490 clean master (negative control) | whisper | none | no cuts | | | 0 s | 0 s | 1 / 44k |

What the raw-recall gaps are: the 4-6 s of jingle after each LdN spot's last spoken word
(not cue-covered), the trailing music sting of the Monday Music Club promo and the first
10 s of the *Net Worth and Chill* promo on Hacks on Tap, whose opening lines read like
editorial. Every boundary the model placed sat on a cue edge within 3 s of the label;
the only editorial cut at all was 1.8 s of inter-cue gap next to two Solved hand-off lines.

Two failure modes were found on the way and are now handled in `podcleaner/detect/llm.py`:

1. **Reasoning budget.** With the backend's default, the model spent an 8000-token output
   budget on hidden reasoning for Solved and Hacks on Tap and returned nothing. Reasoning is
   now disabled for OpenRouter and the budget is 16k; reasoning tokens are reported.
2. **Corrupted structured output.** One LdN490 reply was valid JSON in which the second and
   third segments had been swallowed, escaped, into the first segment's `reason` string,
   followed by a placeholder `{"start_cue": 0, "end_cue": 0}`. `json.loads` accepted it and
   two advertisements vanished silently. Replies are now checked for embedded JSON and
   placeholders and re-asked once with a distinct prompt; a result that stays corrupted is
   marked degraded and fails the test.

Transcription (whisper.cpp small-q5_1, greedy, 120 s windows of the clean master; reference = publisher transcript):

| Window | Ref. words | WER | Timing p90 | Cue openings found | Speech coverage | Realtime factor |
|---|---|---|---|---|---|---|
| LdN491 (de) wer1 | 353 | 4.5 % | 0.70 s | 90 % | 1.00 | 0.51x |
| LdN491 (de) wer2 | 373 | 15.0 % | 0.49 s | 77 % | 1.00 | 0.56x |
| LdN491 (de) wer3 | 338 | 9.8 % | 0.60 s | 87 % | 1.00 | 0.46x |
| LdN490 (de) wer1 | 377 | 13.5 % | 0.18 s | 76 % | 1.00 | 0.59x |
| Solved (en) wer1 | 355 | 10.1 % | 0.28 s | 80 % | 1.00 | 0.64x |
| Solved (en) wer2 | 384 | 8.9 % | 0.53 s | 85 % | 0.95 | 0.63x |
| Solved (en) wer3 | 424 | 3.1 % | 0.37 s | 78 % | 0.95 | 0.60x |

Ad-line anchors: every labelled first/last line inside the anchor windows was found (similarity ≥ 0.91); worst cue-start offset 1.20 s. Drift of a window against the cached whole-episode transcript (Hacks on Tap): 4.3 % away from the window edges (5.4 % raw). Negative controls: 0 words on 30 s of silence, 0 words on 30 s of noise.

Reading the WER column: the two German windows above 13 % are dense political discussion with many proper nouns, and the reference itself is machine-made; the Solved outro window at 3.1 % shows what the same engine does on slow, clear speech. Timing is the better news for cutting: whisper places the opening of a reference cue within 0.2-0.7 s (p90) of the publisher's per-track timestamps.

## 6. Cost: cheaper models and the cascade

Goal set on 2026-09-06: at most 1 cent of LLM spend per episode. A 90-minute episode is
about 60k prompt tokens (Claude tokenizer; other tokenizers count 35-45k), so Sonnet 5
costs 13 cent per episode and the target needs a model at or below about 0.15 USD per
million tokens, or a design that sends most of the transcript to such a model only.

### 6.1 Single cheap models, whole transcript

Live OpenRouter prices, same five cases and gates as the suite
(`tests/integration/compare_models.py`, reports under `var/reports/model-comparison-*`).
None passed:

| Model | $/M in | cent per 90-min episode | What went wrong |
|---|---|---|---|
| anthropic/claude-sonnet-5 (reference) | 2.000 | 12.9 | passes everything |
| qwen/qwen3.7-flash | 0.030 | 0.13 | missed 1 of 3 LdN spots and 3 of 8 Hacks on Tap promos |
| deepseek/deepseek-v4-flash | 0.081 | 0.32 | missed 1 of 8 Hacks on Tap promos |
| z-ai/glm-4.7-flash | 0.060 | 0.23 | missed promos, 1.8 s beyond tolerance |
| openai/gpt-oss-20b / gpt-oss-120b | 0.030 / 0.037 | 0.11 / 0.19 | missed most promos; 120b cut 53 s of editorial |
| openai/gpt-5-nano | 0.050 | 0.50 | cut 35 s of editorial on LdN491 |
| openai/gpt-4o-mini | 0.150 | 0.54 | found 5 of 10 Solved reads, cut 26 s, cut 5 s from the ad-free episode |
| google/gemini-2.5-flash-lite | 0.100 | 0.44 | cut 8 minutes of editorial on Hacks on Tap |
| google/gemini-2.5-flash | 0.300 | 1.37 | cut 50-140 s of editorial; 41k-character replies |
| mistral-nemo, granite-4.0-h-micro, seed-1.6-flash, nemotron-3.5-lightning, qwen3-30b-a3b, mistral-small-3.2 | 0.02-0.08 | 0.1-0.35 | unusable: large editorial cuts or nothing found |
| free tiers (gemma-4, glm-5.2, nemotron-super) | 0 | 0 | rate-limited upstream mid-run; nemotron found 0 of 8 on Hacks on Tap |

Two patterns: the cheap models miss advertising that opens like the programme (the VKB
insurance spot starts with "Verbrennerreform, Energiepreise, Schuldenbremse"; network
promos that never say "sponsor"), and when they do cut, they cut wide.

### 6.2 Screen, then verify

`podcleaner/detect/cascade.py`: a cheap model reads the whole transcript with an
instruction to over-report; a second model sees only the cues from 90 s before to 90 s
after each flagged cluster and decides. Cost is dominated by the cheap pass; the decision
that touches audio is made on a few thousand tokens by the better model. The verifier is
told to leave a doubtful edge cue out.

| Configuration | cent per 90-min episode | cent for the 4 h Solved episode | Verdict |
|---|---|---|---|
| **cascade: qwen3.7-flash -> deepseek-v4-flash** (default) | **0.23** | 0.53 | passes all gates |
| cascade: deepseek-v4-flash -> deepseek-v4-flash | 0.40 | 0.99 | passes |
| cascade: qwen3.7-flash -> gemini-2.5-flash | 0.67 | 1.45 | passes |
| cascade: qwen3.7-flash -> claude-haiku-4.5 | 1.96 | 3.31 | passes, over budget |
| cascade: qwen3.7-flash -> claude-sonnet-5 | 4.68 | 7.51 | passes, over budget |

Every cascade found all 23 labelled segments across the four episodes, with 0 s of
editorial cut beyond the edge tolerance and no cuts on the ad-free episode. The default
is set in `podcleaner/detect/cascade.py` (`DEFAULT_SPEC`) and overridden with
`PODCLEANER_LLM_SPEC` or `pytest --llm-model <spec>`; a bare model id such as
`anthropic/claude-sonnet-5` selects a single-model classifier.

### 6.3 What changed in the evaluation during this search

Be aware of these when reading the pass verdicts. Each was made because a model's
"error" turned out to be a judgement call or a scoring artifact, and each is recorded in
the label sources or the scorer:

- Continuation-marker cues after a splice are don't-care spans (they carry no words).
- Two Solved framing lines were already don't-care; the 0.5-0.8 s pauses between a framing
  line and its ad are now tolerated too, and a segment is credited when the union of
  predictions covers it (a two-spot insert reported as two segments).
- LdN491: the hosts recommending *The Rest is Politics* after it featured their interview
  is labelled ambiguous cross-promo.
- Hacks on Tap: the opening announcement and theme (99-124 s) are labelled ambiguous
  credits, matching the closing credits.

Sonnet passed all gates before any of these; the cheap cascades needed all of them. The
labels still await a human listener (section 2.3). With the shortened-reply prompt
introduced for the cheap models, Sonnet alone placed one LdN491 spot start 10 s late in
its re-run (recall 89.6 % of cue-covered seconds); that run is kept in the reports.

The comparison itself cost about 2.3 USD, most of it on Sonnet and Haiku verifier runs.

## 7. Implication for PodClean itself (verified 2026-09-06)

The User-Agent finding was re-checked with 22 User-Agents, fresh downloads and the detector
itself. What holds:

- **The rule is a denylist of tool clients, not an allowlist of podcatchers.** LdN's CDN
  (`lage.cdn.svmaudio.com`) labels every response: `clean=1` for `curl/*`, `Wget/*` and
  `python-requests/*`; `clean=0` (a pre-built variation or a per-request `livestitch`) for
  everything else, including no User-Agent header at all, Chrome, Safari, VLC, ffmpeg's
  `Lavf/*`, every podcatcher tried, and a `PodClean/2.0` agent. Flightcast (Solved) does the
  same and additionally hands the master to `Lavf/*` and to requests without a User-Agent;
  each podcatcher UA received a differently sized stitched file there.
- **The plain-UA files are stable.** Re-downloads of LdN491, LdN490 and Solved 11 hours after
  pinning were sha256-identical. LdN491's `clean=1` variation dates from 2026-09-01 and is
  the file the publisher transcript was made from.
- **LdN's plain-UA files carry no advertising, not merely no inserted advertising.** The
  default cascade reports zero segments on the full publisher transcripts of LdN491 and
  LdN490 (0.12 cent each); a keyword scan (Werbung, Anzeige, Sponsor, Rabatt, Code, ...)
  finds only editorial uses. The back catalogue behaves the same: LdN392 from August 2024
  has 3 inserted regions (144 s) in its podcatcher file, none in the plain-UA file, and no
  ad words in its transcript. The `PodClean/2.0` download of LdN491 carried the same four
  inserted regions (182 s) as the AntennaPod one.
- **Solved's plain-UA master still contains its host-read spots**: 640 s in 9 segments
  (Notion, AG1, Brain.fm, Surfshark, Momentus, Shopify, Purpose, Waking Up), all found by
  the detector on the clean transcript (0.49 cent). Only the 3 inserted spots (174 s) are
  absent. Hacks on Tap has no fetchable clean master at all.

So a plain-UA fetch removes all ad time on LdN (including old episodes), a fifth of it on
Solved and none on Hacks on Tap, and it only works while PodClean identifies itself as curl
or wget, which the publisher can stop any day. The detector stays necessary. It also
interacts with the `md5(mp3_url)` cache-key risk in the arc42 record: the same URL already
yields different bytes depending on who asks. Whether to rely on it is a product decision,
so it is recorded here and not acted on.

## 8. Honest limits

- WER numbers are against machine references. Treat them as agreement rates, not accuracy.
- Text-derived labels are approximate to a cue edge until a human has listened.
- Hacks on Tap cannot be reproduced by anyone without the original file.
- Inserted ads are geo- and time-targeted; another download at another time gets other ads.
  The suite pins bytes, not URLs, for that reason.
- The classifier's output is not deterministic across model versions; the baselines are
  tied to the model named in the report.
- Claude Sonnet 5 via OpenRouter reasons adaptively by default and, on two of five real
  transcripts, spent an 8000-token output budget on hidden reasoning and returned nothing.
  The client now disables reasoning for OpenRouter (`LLMConfig.reasoning_enabled=False`) and
  budgets 16k output tokens; whether bounded reasoning would improve boundaries is unmeasured.
