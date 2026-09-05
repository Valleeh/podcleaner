"""CLI for a **human** to label ad breaks in real podcast episodes.

This tool does not detect anything and it does not guess.  Every interval in a real
label file must be typed in by a person who listened to the episode.  Per the
verification contract, any file in ``corpus/real/`` asserting hand-labels that nobody
actually made is an automatic failure of step 1 -- so this module deliberately has no
"auto", "suggest" or "import" path, and ``corpus/real/`` ships empty.

Usage::

    python -m podcleaner.eval.label_cli schema
    python -m podcleaner.eval.label_cli init  --episode /path/ep.mp3 --labeler alice
    python -m podcleaner.eval.label_cli add   --label corpus/real/ep.label.json \\
                                              --start 62.5 --end 121.0 --kind host_read
    python -m podcleaner.eval.label_cli finish --label corpus/real/ep.label.json
    python -m podcleaner.eval.label_cli validate corpus/real/ep.label.json
    python -m podcleaner.eval.label_cli list

The schema is also written to ``corpus/real/SCHEMA.json`` in the repo so the format is
documented independently of this code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from podcleaner.eval.corpus import (
    CONVENTION_BREAK_INCLUDING_PAUSE,
    CONVENTION_CREATIVE_ONLY,
    DEFAULT_LABEL_CONVENTION,
    LABEL_CONVENTIONS,
)
from podcleaner.eval.scoring import IntervalError, normalize_intervals

__all__ = [
    "CONVENTION_DESCRIPTIONS",
    "LABEL_CONVENTIONS",
    "LABEL_SCHEMA",
    "LABEL_SUFFIX",
    "LabelError",
    "REAL_CORPUS_DIR",
    "build_stub",
    "gold_from_label",
    "load_label",
    "main",
    "validate_label",
]

LABEL_SUFFIX = ".label.json"

#: Where human labels live.  Ships empty; only ``README.md`` and ``SCHEMA.json`` are
#: committed.
REAL_CORPUS_DIR = Path("corpus/real")

SCHEMA_VERSION = 2

#: What each convention means, in the words a labeller is given. Printed by the CLI so a
#: human never has to guess, and quoted into corpus/real/README.md.
CONVENTION_DESCRIPTIONS = {
    CONVENTION_BREAK_INCLUDING_PAUSE: (
        "The break runs from where the show's audio STOPS to where it RESUMES. "
        "The silence on either side of the ad belongs to the break. "
        "Mark the start at the last moment of real content, not at the first note of "
        "the jingle. THIS IS THE PROJECT DEFAULT -- use it unless told otherwise."
    ),
    CONVENTION_CREATIVE_ONLY: (
        "The break is the advertisement audio ONLY. The silence on either side stays "
        "with the content. Mark the start at the first note of the ad itself."
    ),
}

#: Valid values for ``ads[].kind``.  Descriptive only; the scorer ignores it.
AD_KINDS = ("host_read", "programmatic", "promo", "sponsor_bumper", "other")

LABEL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://podcleaner.invalid/schemas/real-episode-label-v1.json",
    "title": "PodCleaner real-episode ad label",
    "description": (
        "Hand-made ground truth for ONE real podcast episode. Every interval must have "
        "been listened to and typed in by the named human labeler. Machine-generated or "
        "guessed intervals must never be recorded in this format."
    ),
    "type": "object",
    "required": [
        "schema_version",
        "episode",
        "labeler",
        "labeled_at",
        "status",
        "label_convention",
        "ads",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "episode": {
            "type": "object",
            "required": ["audio_path", "duration_seconds"],
            "additionalProperties": False,
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": "Path to the audio the labeler listened to.",
                },
                "sha256": {
                    "type": ["string", "null"],
                    "description": "SHA-256 of that audio file, so a relabel can be "
                    "matched to the exact bytes.",
                },
                "duration_seconds": {"type": "number", "exclusiveMinimum": 0},
                "feed_url": {"type": ["string", "null"]},
                "guid": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
            },
        },
        "labeler": {
            "type": "string",
            "minLength": 1,
            "description": "Identifier of the HUMAN who listened. Not a model name.",
        },
        "labeled_at": {"type": "string", "description": "ISO-8601 timestamp."},
        "status": {
            "enum": ["in_progress", "complete"],
            "description": (
                "'complete' asserts the human listened to the WHOLE episode, so an "
                "absence of intervals means 'no ad here', not 'not checked yet'. Only a "
                "'complete' file may be used as gold for scoring."
            ),
        },
        "label_convention": {
            "enum": list(LABEL_CONVENTIONS),
            "description": (
                "REQUIRED. Which boundary convention the human followed. Ad breaks are "
                "bracketed by silence, and whether that silence counts as part of the "
                "break materially changes the score -- on the synthetic corpus the same "
                "algorithm scored 0.882 vs a 7.778 baseline under one reading and 7.782 "
                "under the other. A corpus that mixes conventions measures labelling "
                "noise, not detector quality, so this field has no default and must be "
                "stated explicitly. "
                + " | ".join(f"{k}: {v}" for k, v in CONVENTION_DESCRIPTIONS.items())
            ),
        },
        "notes": {"type": ["string", "null"]},
        "ads": {
            "type": "array",
            "description": (
                "Ad intervals in seconds from the start of the audio file. Must be "
                "sorted and non-overlapping. May be empty for an episode with no ads."
            ),
            "items": {
                "type": "object",
                "required": ["start", "end"],
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "kind": {"enum": list(AD_KINDS)},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class LabelError(ValueError):
    """Raised when a label file is malformed or when input would fabricate data."""


# --------------------------------------------------------------------------------------
# validation -- deliberately hand-written so `jsonschema` is not a runtime dependency
# --------------------------------------------------------------------------------------


def validate_label(data: object, *, source: str = "<label>") -> dict:
    """Validate one parsed label document.  Returns it unchanged, or raises.

    Checks structure, required keys, and -- via
    :func:`podcleaner.eval.scoring.normalize_intervals` -- that the ad intervals are
    sane and non-overlapping (the same standard the scorer holds *gold* to).
    """
    if not isinstance(data, dict):
        raise LabelError(f"{source}: top level must be an object")

    missing = [k for k in LABEL_SCHEMA["required"] if k not in data]
    if missing:
        raise LabelError(f"{source}: missing required key(s): {', '.join(missing)}")
    unknown = [k for k in data if k not in LABEL_SCHEMA["properties"]]
    if unknown:
        raise LabelError(f"{source}: unknown key(s): {', '.join(sorted(unknown))}")

    if data["schema_version"] != SCHEMA_VERSION:
        raise LabelError(
            f"{source}: schema_version is {data['schema_version']!r}, "
            f"expected {SCHEMA_VERSION}"
        )

    episode = data["episode"]
    if not isinstance(episode, dict):
        raise LabelError(f"{source}: 'episode' must be an object")
    for key in ("audio_path", "duration_seconds"):
        if key not in episode:
            raise LabelError(f"{source}: episode.{key} is required")
    duration = episode["duration_seconds"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise LabelError(f"{source}: episode.duration_seconds must be a number")
    if duration <= 0:
        raise LabelError(f"{source}: episode.duration_seconds must be > 0")

    labeler = data["labeler"]
    if not isinstance(labeler, str) or not labeler.strip():
        raise LabelError(
            f"{source}: 'labeler' must name the human who listened; it is empty"
        )

    convention = data["label_convention"]
    if convention not in LABEL_CONVENTIONS:
        raise LabelError(
            f"{source}: label_convention must be one of {LABEL_CONVENTIONS}, "
            f"got {convention!r}. It has no default on purpose: see "
            f"corpus/real/README.md."
        )

    if data["status"] not in ("in_progress", "complete"):
        raise LabelError(
            f"{source}: status must be 'in_progress' or 'complete', "
            f"got {data['status']!r}"
        )

    ads = data["ads"]
    if not isinstance(ads, list):
        raise LabelError(f"{source}: 'ads' must be a list")
    pairs: List[Sequence[float]] = []
    for i, ad in enumerate(ads):
        if not isinstance(ad, dict):
            raise LabelError(f"{source}: ads[{i}] must be an object")
        for key in ("start", "end"):
            if key not in ad:
                raise LabelError(f"{source}: ads[{i}].{key} is required")
        extra = [k for k in ad if k not in ("start", "end", "kind", "note")]
        if extra:
            raise LabelError(f"{source}: ads[{i}] has unknown key(s): {extra}")
        if "kind" in ad and ad["kind"] not in AD_KINDS:
            raise LabelError(
                f"{source}: ads[{i}].kind must be one of {AD_KINDS}, got {ad['kind']!r}"
            )
        pairs.append((ad["start"], ad["end"]))

    try:
        # Hold human gold to the same standard the scorer does: no overlaps, no NaN,
        # no end < start.
        normalize_intervals(pairs, allow_overlap=False, kind=f"{source}: ads")
    except IntervalError as exc:
        raise LabelError(str(exc)) from exc

    for i, (start, end) in enumerate(pairs):
        if end > duration:
            raise LabelError(
                f"{source}: ads[{i}] ends at {end}s, past the episode duration "
                f"({duration}s)"
            )
    return data


def gold_from_label(
    label: dict, expected_convention: str = DEFAULT_LABEL_CONVENTION
) -> List[List[float]]:
    """Gold intervals from a validated label, refusing a convention mismatch.

    This is the enforcement point the schema field exists for. Scoring a
    ``creative_only`` label against a ``break_including_pause`` corpus (or the reverse)
    silently measures the difference between two labelling policies and calls it detector
    error, so it is refused rather than converted -- the conversion is not possible
    anyway without the pause boundaries, which a human label does not record.

    Also refuses an ``in_progress`` label: ``ads: []`` there means "not checked yet",
    not "no ads".
    """
    validate_label(label)
    if expected_convention not in LABEL_CONVENTIONS:
        raise LabelError(
            f"expected_convention must be one of {LABEL_CONVENTIONS}, "
            f"got {expected_convention!r}"
        )
    if label["label_convention"] != expected_convention:
        raise LabelError(
            f"label was made under convention {label['label_convention']!r} but is "
            f"being scored as {expected_convention!r}. These are not interchangeable "
            f"and cannot be converted after the fact; relabel or score consistently."
        )
    if label["status"] != "complete":
        raise LabelError(
            f"label status is {label['status']!r}; only 'complete' labels may be used "
            f"as gold (an unfinished file's empty 'ads' means 'not checked', not 'no ads')"
        )
    return [[ad["start"], ad["end"]] for ad in label["ads"]]


def load_label(path: Path | str) -> dict:
    """Read and validate a label file."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabelError(f"{p}: not valid JSON: {exc}") from exc
    return validate_label(data, source=str(p))


# --------------------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_stub(
    audio_path: Path | str,
    labeler: str,
    *,
    duration_seconds: Optional[float] = None,
    feed_url: Optional[str] = None,
    guid: Optional[str] = None,
    title: Optional[str] = None,
    label_convention: str = DEFAULT_LABEL_CONVENTION,
    now: Optional[datetime] = None,
) -> dict:
    """Build an EMPTY, ``in_progress`` label document for ``audio_path``.

    ``ads`` starts empty and ``status`` starts ``in_progress`` precisely so that an
    unfinished file cannot be mistaken for "this episode has no ads".
    """
    if not labeler or not labeler.strip():
        raise LabelError("--labeler is required: a human must own the labels")
    if label_convention not in LABEL_CONVENTIONS:
        raise LabelError(
            f"label_convention must be one of {LABEL_CONVENTIONS}, "
            f"got {label_convention!r}"
        )
    audio = Path(audio_path)
    sha = None
    if audio.exists():
        sha = _sha256(audio)
        if duration_seconds is None:
            from podcleaner.eval.corpus import ffprobe_duration_seconds

            duration_seconds = ffprobe_duration_seconds(audio)
    if duration_seconds is None:
        raise LabelError(
            f"{audio} does not exist; pass --duration explicitly if that is intentional"
        )
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "episode": {
            "audio_path": str(audio),
            "sha256": sha,
            "duration_seconds": float(duration_seconds),
            "feed_url": feed_url,
            "guid": guid,
            "title": title,
        },
        "labeler": labeler.strip(),
        "labeled_at": stamp,
        "status": "in_progress",
        "label_convention": label_convention,
        "notes": None,
        "ads": [],
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _cmd_schema(args: argparse.Namespace) -> int:
    print(json.dumps(LABEL_SCHEMA, indent=2))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    stub = build_stub(
        args.episode,
        args.labeler,
        duration_seconds=args.duration,
        feed_url=args.feed_url,
        guid=args.guid,
        title=args.title,
    )
    dest = (
        Path(args.out)
        if args.out
        else Path(args.dir) / (Path(args.episode).stem + LABEL_SUFFIX)
    )
    if dest.exists() and not args.force:
        print(f"refusing to overwrite {dest} (use --force)", file=sys.stderr)
        return 2
    _write(dest, stub)
    print(f"created {dest} (status=in_progress, 0 ad intervals)")
    print("Listen to the episode, then use 'add' for each ad break, then 'finish'.")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    path = Path(args.label)
    data = load_label(path)
    entry = {"start": float(args.start), "end": float(args.end)}
    if args.kind:
        entry["kind"] = args.kind
    if args.note:
        entry["note"] = args.note
    data["ads"] = sorted([*data["ads"], entry], key=lambda a: (a["start"], a["end"]))
    validate_label(data, source=str(path))
    _write(path, data)
    print(f"{path}: now {len(data['ads'])} ad interval(s); status={data['status']}")
    return 0


def _cmd_finish(args: argparse.Namespace) -> int:
    path = Path(args.label)
    data = load_label(path)
    data["status"] = "complete"
    if args.notes:
        data["notes"] = args.notes
    validate_label(data, source=str(path))
    _write(path, data)
    print(
        f"{path}: marked complete with {len(data['ads'])} ad interval(s). "
        "This asserts a human listened to the whole episode."
    )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    failures = 0
    for raw in args.paths:
        try:
            data = load_label(raw)
        except LabelError as exc:
            print(f"INVALID {raw}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(
            f"OK      {raw}: {len(data['ads'])} ad interval(s), "
            f"status={data['status']}, labeler={data['labeler']}"
        )
    return 1 if failures else 0


def _cmd_list(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    found = sorted(directory.glob(f"*{LABEL_SUFFIX}")) if directory.exists() else []
    if not found:
        print(f"{directory}: no label files. Real-episode labels are human work;")
        print("this directory ships empty on purpose. See its README.md.")
        return 0
    for path in found:
        try:
            data = load_label(path)
        except LabelError as exc:
            print(f"INVALID {path}: {exc}")
            continue
        print(f"{path}: {len(data['ads'])} ad(s), status={data['status']}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m podcleaner.eval.label_cli",
        description="Human labelling tool for real podcast episodes. "
        "It never generates labels itself.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("schema", help="print the JSON schema for a label file")
    p.set_defaults(func=_cmd_schema)

    p = sub.add_parser("init", help="create an empty label file for one episode")
    p.add_argument("--episode", required=True, help="path to the audio file")
    p.add_argument("--labeler", required=True, help="who is doing the listening")
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--feed-url", default=None)
    p.add_argument("--guid", default=None)
    p.add_argument("--title", default=None)
    p.add_argument("--dir", default=str(REAL_CORPUS_DIR))
    p.add_argument("--out", default=None, help="explicit output path")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("add", help="record one ad interval a human just heard")
    p.add_argument("--label", required=True)
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--end", type=float, required=True)
    p.add_argument("--kind", choices=AD_KINDS, default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("finish", help="mark a label file complete")
    p.add_argument("--label", required=True)
    p.add_argument("--notes", default=None)
    p.set_defaults(func=_cmd_finish)

    p = sub.add_parser("validate", help="validate label file(s)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("list", help="list label files in the real corpus directory")
    p.add_argument("--dir", default=str(REAL_CORPUS_DIR))
    p.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LabelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
