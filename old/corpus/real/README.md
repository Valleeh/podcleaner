# `corpus/real/` — real episode labels (SHIPS EMPTY)

This directory contains **no labels**, and that is deliberate.

Ground truth for a real podcast episode can only come from a human who listened to the
episode. The verification contract (`docs/verification-contract.md`, criterion S1.6) makes
this explicit:

> Any file asserting hand-labels of a real episode = automatic fail.

...unless a human actually made them. So nothing here is generated, inferred, guessed,
transcribed by a model, or copied from a detector's output. Until a person sits down with
headphones, this directory holds only a schema and a README.

The synthetic corpus (`corpus/synthetic/`, gitignored, built by
`python -m podcleaner.eval.corpus`) is where measurable ground truth comes from today. It
proves the *machinery* works. It cannot prove real-world detection accuracy.

## What lives here once a human has done the work

One file per episode, named `<something>.label.json`, matching `SCHEMA.json`.

## Making a label

```bash
# 1. see the format
python -m podcleaner.eval.label_cli schema

# 2. create an empty label file (status=in_progress, ads=[])
python -m podcleaner.eval.label_cli init \
    --episode /path/to/episode.mp3 --labeler alice

# 3. listen. For each ad break you HEAR, record it:
python -m podcleaner.eval.label_cli add \
    --label corpus/real/episode.label.json \
    --start 62.5 --end 121.0 --kind host_read

# 4. only after listening to the WHOLE episode:
python -m podcleaner.eval.label_cli finish --label corpus/real/episode.label.json

# any time:
python -m podcleaner.eval.label_cli validate corpus/real/episode.label.json
python -m podcleaner.eval.label_cli list
```

## Why `status` matters

A file with `status: "in_progress"` and `ads: []` means *"nobody has finished checking
this"*. A file with `status: "complete"` and `ads: []` means *"a human listened to the
whole thing and there were no ads"*. These are completely different claims and must never
be conflated. **Only `complete` files may be used as gold for scoring.**

## Field reference

See `SCHEMA.json` for the machine-readable version. In short:

| field | meaning |
|---|---|
| `schema_version` | `1` |
| `episode.audio_path` | the file the human listened to |
| `episode.sha256` | hash of those exact bytes, so a relabel can be matched to them |
| `episode.duration_seconds` | full duration; every ad interval must end at or before this |
| `episode.feed_url` / `guid` / `title` | provenance, optional |
| `labeler` | the **human**'s identifier. Not a model name. |
| `labeled_at` | ISO-8601 timestamp |
| `status` | `in_progress` or `complete` — see above |
| `notes` | free text, optional |
| `ads[]` | `{start, end}` seconds, optional `kind` and `note`; sorted, non-overlapping, may be empty |

`ads[].kind` is one of `host_read`, `programmatic`, `promo`, `sponsor_bumper`, `other`.
It is descriptive; the scorer ignores it.

## Interval convention

Seconds from the start of the audio file, `[start, end)`. The same convention
`podcleaner.eval.scoring` uses, so `[[a["start"], a["end"]] for a in label["ads"]]` is
directly usable as the `gold` argument to `score()`.
