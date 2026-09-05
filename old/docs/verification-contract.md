# Verification contract

Binding spec for the rebuild (steps 1–5 of the design memo). Implementer agents and
reviewer agents are both scored against *this file*, not against each other's reports.

## Why this file exists

An agent can write code and write tests that pass for the same wrong reason. Tests passing
is therefore **not** evidence. The three things below are evidence:

1. **Ground truth by construction.** Where possible the answer is known because we *built*
   the input (spliced a known clip at a known offset), not because someone judged it.
2. **Negative controls.** Every criterion has a case that must FAIL / refuse / return nothing.
   A detector that says yes to everything passes a positive-only suite.
3. **Mutation testing.** Each step lists specific bugs to inject. If the suite still passes
   with the bug in, the suite is worthless and the step is NOT done. This is the primary
   anti-circularity check and the reviewer's main job.

## Hard rules (violation = step fails)

- **Never fabricate data.** No invented benchmark numbers, no hand-labels for real podcast
  episodes that nobody labelled, no "example results" presented as measured. If something
  was not run, say it was not run.
- **No network in tests.** Tests must pass with the machine offline. Do not download podcasts.
- **Report real output.** Paste actual pytest output. Never summarise a run you did not do.
- **Run everything with** `/home/valle/podcleaner/.venv/bin/python -m pytest`.
- Do not modify or delete anything under `podcleaner/services/` — the existing pipeline stays
  working. New code is additive.
- ffmpeg/ffprobe are available and are the preferred *independent* oracle for durations:
  verifying our own cut math with our own cut code is circular; verifying it with ffprobe is not.

## Layout

```
podcleaner/core/     db.py states.py queue.py      # step 2
podcleaner/eval/     scoring.py corpus.py label_cli.py   # step 1
podcleaner/detect/   fingerprint.py boundaries.py  # steps 4, 5
podcleaner/feed/     rewrite.py server.py          # step 3
tests/rebuild/       test_<area>.py
corpus/synthetic/    generated, gitignored
corpus/real/         SHIPS EMPTY. schema + labelling CLI only.
```

---

## Step 1 — eval harness, scorer, synthetic corpus

**Goal.** Make quality measurable. A scorer over ad intervals, and a corpus whose ground
truth is exact because it was constructed.

| # | Criterion |
|---|---|
| S1.1 | Hand-computed scorer cases exact: gold `[[10,20]]` — pred `[[10,20]]` → missed 0, false_cut 0. pred `[[10,30]]` → false_cut 10, missed 0. pred `[]` → missed 10, false_cut 0. pred `[[0,30]]` → false_cut 20, missed 0. pred `[[12,18]]` → missed 4, false_cut 0. |
| S1.2 | Interval normalisation is a property, tested with hypothesis: merging is idempotent; merged total ≤ raw total; merged intervals are disjoint and sorted; touching intervals `[0,5],[5,9]` merge to `[0,9]`. |
| S1.3 | Asymmetry is explicit and tested: 1 s of false cut costs exactly `FALSE_CUT_WEIGHT` × 1 s of miss. Test must fail if the weight is removed. |
| S1.4 | Corpus generator splices known ad clips into known content at known offsets and writes a manifest with exact boundaries. **ffprobe** (not our code) confirms output duration = sum of parts within one frame. |
| S1.5 | Negative control: invalid input (end < start, NaN, non-numeric, overlapping *gold*) raises, never silently scores. |
| S1.6 | `corpus/real/` ships EMPTY with a documented JSON schema and a `label_cli.py` for a human. Any file asserting hand-labels of a real episode = automatic fail. |

**Mutations the suite must catch.** (a) swap missed/false_cut in the score formula;
(b) set `FALSE_CUT_WEIGHT = 1`; (c) change merge boundary test from `>=` to `>` so touching
intervals stop merging; (d) off-by-one in the corpus duration sum.

**Honest limit to state in your report.** This proves the *machinery*. It cannot prove
real-world detection accuracy — that needs real labelled episodes, which is human work.

---

## Step 2 — SQLite state machine, leases, workers

**Goal.** An episode survives any crash. This is the memo's central claim; prove it or
report that it does not hold.

States: `discovered → fetched → analyzed → cut → published`, plus `failed`.

| # | Criterion |
|---|---|
| S2.1 | Only legal transitions. Test ALL state pairs; every illegal one raises and leaves the row unmodified. |
| S2.2 | **Headline test.** A real subprocess doing real work is `SIGKILL`ed at each state in turn; after restart the episode still reaches `published`. Parameterized over every state. Not mocked — actually kill a process. |
| S2.3 | Exactly-once: 8 concurrent workers, 50 episodes, each episode's side effect recorded in a log table exactly once. Assert count == 1 for all 50. |
| S2.4 | Lease expiry: while a lease is live no other worker may claim (assert 0 claims); after expiry exactly one claims. |
| S2.5 | `attempts` increments on reclaim; the row moves to `failed` at max attempts and is not retried forever. |
| S2.6 | No `database is locked` under the 8-worker test; WAL enabled; no lost updates. |
| S2.7 | Negative control: two workers must never hold the same lease under contention. |

**Mutations the suite must catch.** (a) drop the `claimed_at` predicate from the claim query
→ S2.3/S2.7 must fail; (b) **commit the next state _before_ doing the work instead of after**
→ S2.2 must fail (this is the classic real bug; a suite that misses it proves nothing);
(c) make the lease never expire → S2.4 must fail.

---

## Step 3 — feed rewriting, degrading redirect, scheduler

Fixture: the repo's real `rss_feed.xml` (135 items, Hacks On Tap).

| # | Criterion |
|---|---|
| S3.1 | **Structural diff oracle.** Parse source and output feeds; assert the ONLY differing nodes are `enclosure@url`, `enclosure@length`, `itunes:duration`. Every other element, attribute and namespace is identical. Rebuilding the feed from scratch must fail this. |
| S3.2 | Episode not ready → HTTP 302, `Location` == the original enclosure URL. Ready → 200, correct bytes, correct `Content-Type`. |
| S3.3 | Degrade-not-fail: a feed where ZERO episodes are cleaned is still valid RSS and still lists all 135 items. |
| S3.4 | Wrong or absent feed token → 404 (not 403 — do not leak existence). |
| S3.5 | Scheduler polls on an interval and enqueues only genuinely new GUIDs; re-polling an unchanged feed enqueues nothing. |
| S3.6 | Negative control: malformed feed → clear error, no partial write, no crash. |

**Mutations.** (a) remove the 302 fallback → S3.2/S3.3 fail; (b) rebuild the feed from
scratch rather than editing in place → S3.1 fails; (c) accept any token → S3.4 fails.

---

## Step 4 — fingerprint tier

All ground truth by construction. Use the step 1 corpus generator.

| # | Criterion |
|---|---|
| S4.1 | Recall: one known ad spliced into 10 content files at random known offsets → all 10 found, offset within ±250 ms. |
| S4.2 | Robustness: the same ad re-encoded at 64/96/128 kbps and at ±3 dB gain still matches. This is fingerprinting's actual claim. |
| S4.3 | **False positives — the criterion that matters most.** 20 content files containing NO ad → ZERO matches. A false positive causes a bad cut, which the memo ranks as the worst outcome. |
| S4.4 | Compounding: after a creative is confirmed once, the next episode containing it matches with the LLM path invoked exactly 0 times (assert on a call-counting stub). |
| S4.5 | Determinism: identical input → byte-identical fingerprint. |

**Mutations.** (a) loosen the match threshold to always-match → S4.3 fails; (b) tighten to
never-match → S4.1 fails; (c) break the offset arithmetic → S4.1's offset assertion fails.

---

## Step 5 — boundary snapping and the refusal rule

| # | Criterion |
|---|---|
| S5.1 | Synthetic audio with silence at a known t → the snapped boundary lands within tolerance of t. |
| S5.2 | **Core policy, negative control.** No silence within tolerance → the segment is KEPT, not cut. |
| S5.3 | A cut boundary never falls strictly inside a word interval, given a word-timestamp fixture. |
| S5.4 | **ffprobe oracle.** Output duration == input duration − Σ(cut durations), within one frame. Verified with ffprobe, not with our own cutting code. |
| S5.5 | Chapter marks shift correctly: a chapter at T, after a cut of D ending before T, lands at T − D. Hand-computable. |
| S5.6 | Regression gate: the step 1 asymmetric score on the synthetic corpus does not worsen against a no-snapping baseline. Report both numbers. |

**Mutations.** (a) always cut regardless of edge quality → S5.2 fails; (b) snap in the wrong
direction → S5.1 fails; (c) skip the chapter shift → S5.5 fails.

---

## Definition of done, per step

1. Every criterion has a test that actually runs and passes — real pasted output.
2. Every listed mutation was injected, the suite was re-run, and the expected test FAILED.
   Record which test caught which mutation, then revert the mutation.
3. Anything not achieved is stated plainly as not achieved. A partial step honestly reported
   is worth more than a complete-looking one that is not.
