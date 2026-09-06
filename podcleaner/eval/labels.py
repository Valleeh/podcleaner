"""Ground-truth label files for real episodes (schema version 3).

A label file records, for **one specific audio file** (pinned by SHA-256), every
advertising segment in that file's own timeline, together with where each segment's
boundaries came from.  Provenance is the point of the schema:

``source`` per segment
    ``construction``  -- derived by :mod:`podcleaner.eval.dai` from the clean and the
    stitched file; exact to one MP3 frame; nobody judged anything.
    ``text``          -- a reader located the boundaries in a transcript (cue-aligned);
    approximate to the cue edge; needs a human to confirm by listening.
    ``listened``      -- a human heard it and typed the times.

``status`` per file
    ``in_progress`` -- not every segment is verified; the file must not be used as gold
    unless the caller explicitly asks for provisional scoring.
    ``complete``    -- the named human has verified every ``text`` segment by listening
    and asserts there are no further ads.  Only then is the file gold.

``ambiguous`` per segment marks judgement calls (a guest plugging their own show, the
framing sentence before a break).  The scorer treats those spans as don't-care.

Conventions.  Boundaries are ``cue_aligned`` (the start of the first and the end of the
last transcript cue that belongs to the ad) unless the segment was constructed, in which
case they are the splice points and the whole insert -- jingle, silence and all --
is the break (``break_including_pause``).

CLI::

    python -m podcleaner.eval.labels validate tests/integration/labels/*.label.json
    python -m podcleaner.eval.labels checklist tests/integration/labels/solved.label.json
    python -m podcleaner.eval.labels verify  --label FILE --labeler NAME --all
    python -m podcleaner.eval.labels finish  --label FILE --labeler NAME
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from podcleaner.eval.adscore import GoldAd
from podcleaner.eval.scoring import IntervalError, normalize_intervals
from podcleaner.transcripts import format_clock

__all__ = [
    "AD_CATEGORIES",
    "LABEL_SUFFIX",
    "SCHEMA_VERSION",
    "SOURCES",
    "LabelError",
    "checklist",
    "gold_ads",
    "load_label",
    "new_label",
    "save_label",
    "validate_label",
]

PathLike = Union[str, Path]

SCHEMA_VERSION = 3
LABEL_SUFFIX = ".label.json"

#: Segment categories; the first five match :data:`podcleaner.detect.llm.CATEGORIES`.
AD_CATEGORIES = (
    "sponsor_read",
    "host_endorsement",
    "cross_promo",
    "self_promo",
    "credits",
    "other",
)
SOURCES = ("construction", "text", "listened")
CONVENTIONS = ("cue_aligned", "break_including_pause", "creative_only")
STATUSES = ("in_progress", "complete")

_FILE_KEYS = {
    "schema_version", "episode", "transcript", "provenance", "labeler", "labeled_at",
    "status", "label_convention", "notes", "ads",
}
_EPISODE_KEYS = {
    "id", "podcast", "title", "guid", "feed_url", "enclosure_url", "audio_file", "sha256",
    "duration_seconds", "variant", "user_agent",
}
_AD_KEYS = {
    "start", "end", "category", "inserted", "ambiguous", "source", "verified", "start_cue",
    "end_cue", "first_line", "last_line", "note",
}


class LabelError(ValueError):
    """Malformed label file, or an attempt to use unfinished labels as gold."""


def _num(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LabelError(f"{name} must be a number, got {value!r}")
    return float(value)


def validate_label(data: object, *, source: str = "<label>") -> dict:
    """Structural validation.  Returns ``data`` unchanged or raises :class:`LabelError`."""
    if not isinstance(data, dict):
        raise LabelError(f"{source}: top level must be an object")
    missing = [k for k in ("schema_version", "episode", "provenance", "status", "label_convention", "ads") if k not in data]
    if missing:
        raise LabelError(f"{source}: missing required key(s): {', '.join(missing)}")
    unknown = sorted(set(data) - _FILE_KEYS)
    if unknown:
        raise LabelError(f"{source}: unknown key(s): {', '.join(unknown)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise LabelError(f"{source}: schema_version {data['schema_version']!r}, expected {SCHEMA_VERSION}")

    ep = data["episode"]
    if not isinstance(ep, dict):
        raise LabelError(f"{source}: 'episode' must be an object")
    unknown = sorted(set(ep) - _EPISODE_KEYS)
    if unknown:
        raise LabelError(f"{source}: episode has unknown key(s): {', '.join(unknown)}")
    for key in ("id", "audio_file", "sha256", "duration_seconds"):
        if key not in ep:
            raise LabelError(f"{source}: episode.{key} is required")
    duration = _num(ep["duration_seconds"], f"{source}: episode.duration_seconds")
    if duration <= 0:
        raise LabelError(f"{source}: episode.duration_seconds must be > 0")
    if not isinstance(ep["sha256"], str) or len(ep["sha256"]) != 64:
        raise LabelError(f"{source}: episode.sha256 must be a 64-hex-character string")

    prov = data["provenance"]
    if not isinstance(prov, dict) or not isinstance(prov.get("method"), str):
        raise LabelError(f"{source}: provenance.method is required")
    if data["status"] not in STATUSES:
        raise LabelError(f"{source}: status must be one of {STATUSES}")
    if data["label_convention"] not in CONVENTIONS:
        raise LabelError(f"{source}: label_convention must be one of {CONVENTIONS}")
    if data["status"] == "complete":
        labeler = data.get("labeler")
        if not isinstance(labeler, str) or not labeler.strip():
            raise LabelError(f"{source}: a complete label must name the human labeler")

    ads = data["ads"]
    if not isinstance(ads, list):
        raise LabelError(f"{source}: 'ads' must be a list")
    pairs: List[Tuple[float, float]] = []
    for i, ad in enumerate(ads):
        if not isinstance(ad, dict):
            raise LabelError(f"{source}: ads[{i}] must be an object")
        unknown = sorted(set(ad) - _AD_KEYS)
        if unknown:
            raise LabelError(f"{source}: ads[{i}] has unknown key(s): {', '.join(unknown)}")
        for key in ("start", "end", "category", "source"):
            if key not in ad:
                raise LabelError(f"{source}: ads[{i}].{key} is required")
        start = _num(ad["start"], f"{source}: ads[{i}].start")
        end = _num(ad["end"], f"{source}: ads[{i}].end")
        if ad["category"] not in AD_CATEGORIES:
            raise LabelError(f"{source}: ads[{i}].category must be one of {AD_CATEGORIES}")
        if ad["source"] not in SOURCES:
            raise LabelError(f"{source}: ads[{i}].source must be one of {SOURCES}")
        for flag in ("inserted", "ambiguous", "verified"):
            if flag in ad and not isinstance(ad[flag], bool):
                raise LabelError(f"{source}: ads[{i}].{flag} must be a boolean")
        if end > duration + 0.5:
            raise LabelError(f"{source}: ads[{i}] ends at {end}s, past the episode duration ({duration}s)")
        pairs.append((start, end))
        if data["status"] == "complete" and ad["source"] == "text" and not ad.get("verified", False):
            raise LabelError(
                f"{source}: status is complete but ads[{i}] (text-derived, {format_clock(start)}) "
                f"is not verified; a human must listen to it first"
            )
    try:
        # ambiguous framing segments may touch their ad, but nothing may overlap
        normalize_intervals(pairs, allow_overlap=False, kind=f"{source}: ads")
    except IntervalError as exc:
        raise LabelError(str(exc)) from exc
    return data


def load_label(path: PathLike) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabelError(f"{p}: not valid JSON: {exc}") from exc
    return validate_label(data, source=str(p))


def save_label(path: PathLike, data: dict) -> None:
    validate_label(data, source=str(path))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def new_label(
    *,
    episode: dict,
    provenance: dict,
    ads: Sequence[dict],
    transcript: Optional[dict] = None,
    label_convention: str = "cue_aligned",
    notes: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Assemble a label document.  ``status`` starts ``in_progress`` unless every
    segment is constructed or already verified."""
    ads = [dict(a) for a in sorted(ads, key=lambda a: (a["start"], a["end"]))]
    for a in ads:
        a.setdefault("inserted", False)
        a.setdefault("ambiguous", False)
        a.setdefault("verified", a["source"] == "construction")
    complete = all(a["source"] != "text" or a["verified"] for a in ads)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "episode": dict(episode),
        "transcript": dict(transcript) if transcript else None,
        "provenance": dict(provenance),
        "labeler": None,
        "labeled_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        # Even an all-constructed file starts in_progress: 'complete' also asserts that a
        # human confirmed the episode has no *other* (baked-in) advertising.
        "status": "in_progress",
        "label_convention": label_convention,
        "notes": notes,
        "ads": ads,
    }
    doc["provenance"].setdefault("all_segments_verified", complete)
    return validate_label(doc)


def gold_ads(
    label: dict,
    *,
    allow_provisional: bool = False,
    audio_sha256: Optional[str] = None,
) -> List[GoldAd]:
    """The label's segments as :class:`GoldAd` objects.

    Refuses an ``in_progress`` label unless ``allow_provisional`` is set -- an unfinished
    file's segments are candidates, not truth.  Refuses a label whose pinned audio hash
    differs from ``audio_sha256`` when given: labels are meaningless against different bytes
    (dynamically inserted ads move between downloads).
    """
    validate_label(label)
    if label["status"] != "complete" and not allow_provisional:
        raise LabelError(
            f"label for {label['episode'].get('id')} is {label['status']!r}; only complete labels "
            f"are gold (pass allow_provisional=True to score against candidates)"
        )
    if audio_sha256 is not None and label["episode"]["sha256"] != audio_sha256:
        raise LabelError(
            f"label for {label['episode'].get('id')} pins sha256 {label['episode']['sha256'][:12]}... "
            f"but the audio has {audio_sha256[:12]}...; these labels do not apply to this file"
        )
    return [
        GoldAd(
            start=float(a["start"]),
            end=float(a["end"]),
            category=a["category"],
            ambiguous=bool(a.get("ambiguous", False)),
            note=a.get("note") or a.get("first_line") or "",
        )
        for a in label["ads"]
    ]


def checklist(label: dict) -> str:
    """Human verification checklist: what to listen to, and what to expect there."""
    ep = label["episode"]
    lines = [
        f"# Verification checklist: {ep.get('podcast', '')} - {ep.get('title', ep.get('id'))}",
        "",
        f"File: {ep['audio_file']}  (sha256 {ep['sha256'][:16]}...)",
        f"Status: {label['status']}   Labeler: {label.get('labeler') or '-'}",
        f"Provenance: {label['provenance'].get('method')} -- {label['provenance'].get('notes', '')}",
        "",
        "For every segment below, listen from about 5 s before the start to 5 s after it,",
        "and from 5 s before the end to 5 s after it. Confirm that the boundary falls",
        "between the ad and the programme and that the quoted lines are what you hear.",
        "Then mark it: `python -m podcleaner.eval.labels verify --label <file> --labeler <you> --ad <n>`.",
        "Constructed segments (source=construction) are exact splice points and need no",
        "listening; the whole file needs one pass for advertising nobody has labelled yet.",
        "",
    ]
    for n, a in enumerate(label["ads"]):
        flags = []
        if a.get("inserted"):
            flags.append("inserted")
        if a.get("ambiguous"):
            flags.append("ambiguous / don't-care")
        if a.get("verified"):
            flags.append("verified")
        lines.append(
            f"{n:2d}. {format_clock(a['start'])} -> {format_clock(a['end'])}  "
            f"({a['end'] - a['start']:.1f}s)  {a['category']}  [{a['source']}] "
            f"{' '.join(f'<{f}>' for f in flags)}"
        )
        if a.get("first_line"):
            lines.append(f"      starts: \"{a['first_line']}\"")
        if a.get("last_line"):
            lines.append(f"      ends:   \"{a['last_line']}\"")
        if a.get("note"):
            lines.append(f"      note:   {a['note']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _cmd_validate(args) -> int:
    failures = 0
    for raw in args.paths:
        try:
            data = load_label(raw)
        except LabelError as exc:
            print(f"INVALID {raw}: {exc}", file=sys.stderr)
            failures += 1
            continue
        n_text = sum(1 for a in data["ads"] if a["source"] == "text")
        n_ver = sum(1 for a in data["ads"] if a["source"] == "text" and a.get("verified"))
        print(f"OK      {raw}: {len(data['ads'])} segment(s), {n_ver}/{n_text} text-derived verified, "
              f"status={data['status']}, labeler={data.get('labeler') or '-'}")
    return 1 if failures else 0


def _cmd_checklist(args) -> int:
    print(checklist(load_label(args.label)), end="")
    return 0


def _cmd_verify(args) -> int:
    path = Path(args.label)
    data = load_label(path)
    if not args.labeler.strip():
        raise LabelError("--labeler must name the human who listened")
    indices = range(len(data["ads"])) if args.all else args.ad
    if not indices:
        raise LabelError("pass --ad N (repeatable) or --all")
    for n in indices:
        if n < 0 or n >= len(data["ads"]):
            raise LabelError(f"no segment {n}; the file has {len(data['ads'])}")
        data["ads"][n]["verified"] = True
    data["labeler"] = args.labeler.strip()
    data["labeled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_label(path, data)
    print(f"{path}: marked {len(list(indices))} segment(s) verified by {data['labeler']}")
    return 0


def _cmd_finish(args) -> int:
    path = Path(args.label)
    data = load_label(path)
    data["labeler"] = args.labeler.strip()
    data["status"] = "complete"
    data["labeled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.notes:
        data["notes"] = args.notes
    save_label(path, data)  # validation refuses unverified text segments
    print(f"{path}: complete. This asserts that {data['labeler']} verified every text-derived "
          f"segment by listening and heard no other advertising in the file.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m podcleaner.eval.labels", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("validate"); s.add_argument("paths", nargs="+"); s.set_defaults(func=_cmd_validate)
    s = sub.add_parser("checklist"); s.add_argument("label"); s.set_defaults(func=_cmd_checklist)
    s = sub.add_parser("verify"); s.add_argument("--label", required=True); s.add_argument("--labeler", required=True)
    s.add_argument("--ad", type=int, action="append", default=[]); s.add_argument("--all", action="store_true")
    s.set_defaults(func=_cmd_verify)
    s = sub.add_parser("finish"); s.add_argument("--label", required=True); s.add_argument("--labeler", required=True)
    s.add_argument("--notes"); s.set_defaults(func=_cmd_finish)
    args = p.parse_args(argv)
    try:
        return int(args.func(args))
    except LabelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
