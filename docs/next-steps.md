# Status and next steps (2026-09-06)

Snapshot of the v2 detection work after the integration-test and cost sessions. Update or
delete this file once the items below are done; the design record is
`docs/integration-tests.md`, the architecture is `docs/architecture.md`.

## 1. Where things stand

**Nothing is committed.** Branch `docs/arc42-architecture`; `git status` shows `podcleaner/`,
`tests/`, `pyproject.toml` and `docs/integration-tests.md` untracked and `.gitignore` modified
(adds `.secret.json`). `var/` (fixtures, caches, reports; ~840 MB) is gitignored and must stay
that way: the audio is commercial.

**What exists in `podcleaner/` (v2)**

| Module | Role |
|---|---|
| `transcripts.py` | `Transcript`/`Cue`/`Word`, SRT/VTT/whisper-JSON parsing, windows and shifting |
| `detect/transcribe.py` | whisper.cpp via the `whisper-cpp:local` Docker image, window transcription, content-hash cache |
| `detect/llm.py` | Transcript chunking, cue-index prompt, OpenAI-compatible client (OpenRouter), reply repair and retry, `AdAnalysis.cut_intervals` |
| `detect/cascade.py` | Screen-then-verify cascade; `DEFAULT_SPEC = cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash` |
| `eval/dai.py` | Exact inserted-ad regions from clean vs ad-stitched MP3 (frame walk) |
| `eval/adscore.py`, `eval/wer.py`, `eval/labels.py`, `eval/fixtures.py`, `eval/pricing.py` | Scorer, WER/timing metrics, label schema v3 + CLI, sha256-pinned fixture store, OpenRouter price catalogue |

The rest of the arc42 building blocks (job registry, WorkQueue, worker, stage handlers,
fingerprint tier, boundary snapping, feed server) still live only under `old/podcleaner/`.
The v2 slice is a library with tests; it is not yet reachable from any pipeline.

**Tests**

- `pytest` alone runs `tests/unit` offline (last run: 86 passed, 41 integration tests skipped
  by design).
- `pytest --integration` runs `tests/integration/` against four pinned episodes (LdN491, LdN490,
  Solved "Finding Your Life Path", Hacks on Tap "Oh Canada"). Flags and run lines are in
  `tests/integration/README.md`. Whisper and LLM suites must run one after the other on this
  2 GB host.

**Measured (details and tables in `docs/integration-tests.md`, sections 5 and 6)**

- Transcription, whisper.cpp small-q5_1 on 3-minute windows: all gates met (window WER under
  the 0.30 de / 0.25 en ceilings, word-timing p90 under 1.5 s, ad anchor lines within 3 s,
  no drift over long spans, near-silent negatives produce at most 2 words).
- Ad detection against the provisional labels, all four episodes plus the ad-free negative
  control:

| Spec | Cent per 90-min episode | Cent, 4 h Solved | Gates |
|---|---|---|---|
| `cascade:qwen/qwen3.7-flash>deepseek/deepseek-v4-flash` (default) | 0.23 | 0.53 | all pass |
| `cascade:deepseek/deepseek-v4-flash>deepseek/deepseek-v4-flash` | 0.40 | 0.99 | all pass |
| `cascade:qwen/qwen3.7-flash>google/gemini-2.5-flash` | 0.67 | 1.45 | all pass |
| `anthropic/claude-sonnet-5` alone | 12.9 | 27.6 | all pass |

  No single model under 0.30 USD per million input tokens passed on its own. Baselines for the
  default cascade are recorded in `tests/integration/baselines.json`.

**Money spent this session:** about 5 USD on OpenRouter, 2.3 USD of it on the model
comparison. Cached replies under `var/cache/llm` (keyed by model config and prompts) make
re-runs free until a prompt or model changes.

## 2. What is provisional

1. **No human has listened to any label.** All four label files are `in_progress`. 34 of the
   44 segments are text-derived and need a listening pass; the other 10 are exact splice
   points from the DAI oracle. The ad-detection tests only score them with
   `--provisional-labels`.
2. **Four scoring and label refinements were made during the cheap-model search**
   (continuation cues at splice points are don't-care; a stacked break reported as two
   segments counts as found; pauses up to 3 s between a framing line and its ad are
   tolerated; "The Rest is Politics" on LdN491 and the Hacks on Tap opening announcement are
   ambiguous). Sonnet passed before them; the cheap cascades needed all of them. Each is
   argued in `docs/integration-tests.md` section 6, but a second opinion is due.
3. **Sonnet is no longer an untouched reference.** The shorter-reply prompt introduced for the
   cheap models made Sonnet place one LdN491 spot start 10 s late on re-run.
4. **Whole-episode whisper transcripts exist only for LdN490 and Hacks on Tap** (imported from
   `~/whisper-transcribe`). LdN491 and Solved are scored on hybrid transcripts (publisher
   cues shifted around whisper-transcribed inserts).

## 3. Next steps, in order

1. **Commit.** Nothing here is saved in git. Suggested split: the v2 slice with its unit
   tests; the integration harness with manifest, DAI regions, labels and baselines; the
   docs. Check `git status` shows no `var/` path before each commit. Decide whether this
   belongs on `docs/arc42-architecture` or a new branch off `master`.

2. **Verify the labels by listening** (owner's work, the checklists tell you exactly where):

   ```
   source .venv/bin/activate
   python -m podcleaner.eval.labels checklist tests/integration/labels/ldn491.podcatcher.label.json
   python -m podcleaner.eval.labels verify --label tests/integration/labels/ldn491.podcatcher.label.json --labeler <you> --ad 3
   python -m podcleaner.eval.labels finish --label tests/integration/labels/ldn491.podcatcher.label.json --labeler <you>
   ```

   Same for `ldn490`, `solved-life-path` (17 segments) and `hot-oh-canada` (16). Decide the
   ambiguous ones while there: Rest-is-Politics (LdN491 #3), the inserted self-promo at the
   end of LdN491 (#4), the Hacks on Tap intro and theme, closing credits. Each file also
   needs one pass over the whole episode for advertising nobody has labelled; reading the
   transcript and listening at anything ad-like is the realistic way to do 8 hours of audio.
   Caution: the label files are generated by `tests/integration/labels/build_labels.py`; if a
   verdict changes, put it into `labels/sources/` and check that re-running the builder keeps
   the `verified` flags rather than clobbering them.

3. **Re-score on finished labels** and re-record: `pytest --integration
   tests/integration/test_ad_detection.py --record-baselines` without `--provisional-labels`.
   If a human verdict flips one of the ambiguous segments, the cheap cascade's pass may flip
   with it; `python tests/integration/compare_models.py -m <spec> ...` re-ranks the
   alternatives from the table above without new labelling work.

4. **Review the scoring refinements against arc42 8.9.** Add gate-level mutation checks to
   the integration harness: perturb a cached prediction (shift an edge by 10 s, drop a
   segment, add a 5 s cut inside editorial) and assert the corresponding gate fails. The
   scorer's unit tests cover the semantics; nothing yet proves the assembled gates still bite.

5. **Wire the slice into the analyze stage** (arc42 section 5.2). Port `core/`
   (db, queue, states), `detect/fingerprint.py`, `detect/boundaries.py` and the feed server
   from `old/podcleaner/` into `podcleaner/`, then connect transcriber → classifier →
   boundaries: the classifier from `classifier_from_spec` yields an `AdAnalysis` whose
   `cut_intervals` are the input to boundary snapping and the refuse-to-cut rule (ADR-8).
   Move the host-specific defaults (`WhisperConfig` model path and Docker image, memory cap,
   LLM spec, secrets file) into configuration per arc42 8.7.

6. **Run the whole-episode transcriptions** for LdN491 and Solved, one at a time, overnight:
   `pytest --integration --full tests/integration/test_transcription.py` (whisper takes about
   1.6 times the audio length here: about 2.5 h for LdN491, 6.5 h for Solved). That unlocks the
   pure-whisper ad-detection cases and full-episode WER baselines.

7. **Make the cost path robust.** The default depends on two third-party models behind
   OpenRouter; configure a fallback spec (deepseek→deepseek at 0.40 cent) for rate limits or
   model removal, and re-run the comparison when OpenRouter's price list changes
   (`podcleaner/eval/pricing.py` caches it). Whether bounded reasoning would tighten
   boundaries is still unmeasured.

8. **Widen the corpus.** Four episodes from three shows is thin. Add episodes with host-read
   ads that are not server-inserted, a second ad-free negative, and another publisher that
   serves a clean master to a plain User-Agent (re-pinning steps are in the README).

9. **Small fixes.** The docs say `python -m ...`; this host has no `python` on PATH, only
   `.venv/bin/python`, so the run lines need `source .venv/bin/activate` first.

## 4. Open decisions for the owner

- **Fetch with a plain User-Agent?** Verified 2026-09-06 (integration-test record, section
  7): LdN and Flightcast serve the ad-free master only to curl, wget and python-requests; a
  `PodClean/2.0` agent, a browser, or a request without a User-Agent gets stitched ads. It
  would remove all ad time on LdN (back catalogue included), a fifth on Solved (the host
  reads stay) and nothing on Hacks on Tap, and it depends on impersonating curl, which the
  publisher can stop any day. It also interacts with the `md5(mp3_url)` cache-key risk in
  arc42 section 11.
- **Which ambiguous segments count as ads** for PodClean's own policy: host recommendations
  of other shows, network cross-promos, opening announcements, closing credits. The labels
  carry them as don't-care until this is decided.
- **Is the cheap cascade the product default, or a cost tier below Sonnet?** Both pass
  today's gates; only the cascade meets the 1 cent target.
