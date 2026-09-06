# Integration tests: real episodes

These tests run the v2 detection slice (`podcleaner/detect/`) against three real podcasts
and measure what comes out. They are **off by default**: `pytest` alone runs only
`tests/unit`. See `docs/integration-tests.md` for the design, the data provenance and the
measured results, and `docs/next-steps.md` for the current status and open work.

```
pytest --integration                                   # everything that can run on this host
pytest --integration --download                        # fetch missing fixtures first (~750 MB)
pytest --integration tests/integration/test_transcription.py     # whisper only (~40 min)
pytest --integration tests/integration/test_ad_detection.py      # LLM only (OPENROUTER_API_KEY, or .secret.json with "openrouter-token")
pytest --integration --provisional-labels              # score against labels no human has finished yet
pytest --integration --record-baselines                # write measured metrics to baselines.json
pytest --integration --full                            # whole-episode transcription (hours)
pytest --integration --llm-fresh                       # ignore cached LLM replies (re-bills)
pytest --integration --llm-model anthropic/claude-sonnet-5           # single model instead of the default cascade
pytest --integration --llm-model "cascade:qwen/qwen3.7-flash>google/gemini-2.5-flash"
python tests/integration/compare_models.py -m <spec> -m <spec>       # cost/quality table across models
```

**Run the whisper suite and the LLM suite one after the other, never at the same time.**
This host has 2 GB of RAM; a whisper container next to a pytest process holding a four-hour
transcript was killed for memory during development.

Tests that cannot run here **skip with the reason** (no Docker image, no API key, missing
fixture, hash mismatch, unverified labels). They never pass vacuously.

## Files

| Path | What |
|---|---|
| `manifest.json` | Every pinned fixture: episodes, audio variants, transcripts, windows, sha256s |
| `dai/*.json` | Inserted-ad regions from `podcleaner.eval.dai` (exact, by construction) |
| `labels/*.label.json` | Ground truth per file (schema v3, with provenance and verification state) |
| `labels/*.VERIFY.md` | What a human must listen to before a label file may be marked complete |
| `labels/sources/` | Inputs to `labels/build_labels.py`: text-derived cue ranges, category overrides, the imported Hacks on Tap annotation |
| `baselines.json` | Measured metrics the regression gates are derived from |
| `support.py` | Hybrid-transcript builder, LLM reply cache, report/baseline plumbing |

Audio, transcripts and caches live under `var/fixtures/` and `var/cache/` (gitignored:
commercial podcasts). Reports land in `var/reports/<timestamp>/`.

## Verifying the labels (human work)

```
python -m podcleaner.eval.labels checklist tests/integration/labels/solved-life-path.podcatcher.label.json
# listen to each boundary listed, then:
python -m podcleaner.eval.labels verify --label tests/integration/labels/solved-life-path.podcatcher.label.json --labeler <you> --ad 3 --ad 4
python -m podcleaner.eval.labels finish --label tests/integration/labels/solved-life-path.podcatcher.label.json --labeler <you>
```

`finish` refuses while any text-derived segment is unverified. Until a file is complete,
the ad-detection tests skip it unless `--provisional-labels` is passed.

## Re-pinning

A fresh download of any of these episodes is a *different file* (server-side ad
insertion), so labels never transfer. To pin a new episode: download the clean variant
(plain `curl`) and the podcatcher variant (`-A AntennaPod/3.5.0`), run
`python -m podcleaner.eval.dai clean.mp3 stitched.mp3 --json tests/integration/dai/<id>.json`,
add the episode to `manifest.json` with both sha256s, describe any baked-in ads in
`labels/sources/<id>.text-ads.json`, and run `python tests/integration/labels/build_labels.py`.
