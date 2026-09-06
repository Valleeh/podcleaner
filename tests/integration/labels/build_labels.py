"""Build the label files under tests/integration/labels/ from their sources.

Sources, in order of trust:

1. **Constructed** inserted-ad regions from the DAI oracle (``tests/integration/dai/*.json``):
   exact splice points, no judgement involved.
2. **Text-derived** segments (``labels/sources/*.text-ads.json``): cue ranges a reader located
   in a transcript.  Boundaries are cue edges; a human must confirm them by listening.
3. **Converted** annotation from the whisper-transcribe repo for Hacks on Tap.

Text quotes for constructed regions come from whisper transcripts of exactly those regions
(cached under var/cache), so the checklist can say what a listener should hear there.

Re-run after changing a source::

    .venv/bin/python tests/integration/labels/build_labels.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from podcleaner.detect.transcribe import TranscriptionError, WhisperCppTranscriber  # noqa: E402
from podcleaner.eval.dai import DaiResult, InsertedRegion  # noqa: E402
from podcleaner.eval.fixtures import FixtureStore, load_manifest  # noqa: E402
from podcleaner.eval.labels import checklist, new_label, save_label  # noqa: E402
from podcleaner.transcripts import Transcript, load_transcript  # noqa: E402

DRAFTED_BY = "Claude (Fable 5.1), 2026-09-06, from transcript text and the DAI oracle; no listening"


def load_dai(path: Path) -> DaiResult:
    d = json.loads(path.read_text())
    regions = [
        InsertedRegion(r["start"], r["end"], r["start_frame"], r["end_frame"], r["byte_start"], r["byte_end"],
                       r.get("skipped_clean_frames", 0), r.get("skipped_clean_seconds", 0.0))
        for r in d["regions"]
    ]
    res = DaiResult(regions, d["clean_duration"], d["stitched_duration"], d["clean_frames"], d["stitched_frames"],
                    d["matched_frames"], d.get("modified_frames", 0), d.get("clean_path"), d.get("stitched_path"))
    shift = 0.0
    for r in regions:
        res.offset_map.append((r.start - shift, shift + r.duration))
        shift += r.duration
    return res


def region_quotes(transcriber, audio: Path, region: InsertedRegion, language: str):
    """First and last line whisper hears inside an inserted region (cache-only: never
    starts a transcription here, the build must stay fast and offline)."""
    try:
        t = transcriber.transcribe(audio, start=region.start, duration=region.duration, language=language)
    except TranscriptionError:
        return None, None
    spoken = [c.text for c in t.cues if c.text.strip()]
    if not spoken:
        return None, None
    return spoken[0], spoken[-1]


def constructed_ads(dai: DaiResult, transcriber, audio: Path, language: str, overrides: list):
    by_start = {round(o["start"], 3): o for o in overrides}
    ads = []
    for r in dai.regions:
        first, last = region_quotes(transcriber, audio, r, language)
        o = by_start.get(round(r.start, 3), {})
        note = f"server-inserted; splice frames {r.start_frame}-{r.end_frame}"
        if r.skipped_clean_frames:
            note += f"; {r.skipped_clean_frames} clean frame(s) rewritten at the splice"
        if o.get("note"):
            note = o["note"] + " (" + note + ")"
        ads.append({
            "start": round(r.start, 3), "end": round(r.end, 3), "category": o.get("category", "sponsor_read"),
            "inserted": True, "ambiguous": bool(o.get("ambiguous", False)), "source": "construction", "verified": True,
            "first_line": first, "last_line": last, "note": note,
        })
    return ads


def text_ads(spec: dict, transcript: Transcript, dai: DaiResult | None):
    by_index = {c.index: c for c in transcript.cues}
    ads = []
    for s in spec["segments"]:
        first, last = by_index[s["start_cue"]], by_index[s["end_cue"]]
        start, end = first.start, last.end
        if dai is not None:
            start, end = dai.to_stitched(start), dai.to_stitched(end)
        ads.append({
            "start": round(start, 3), "end": round(end, 3), "category": s["category"],
            "inserted": False, "ambiguous": bool(s.get("ambiguous", False)), "source": "text", "verified": False,
            "start_cue": s["start_cue"], "end_cue": s["end_cue"],
            "first_line": first.text, "last_line": last.text, "note": s.get("note"),
        })
    return ads


def build_dai_episode(eid: str, episodes, store, transcriber, spec_path: Path | None):
    ep = episodes[eid]
    dai = load_dai(HERE.parent / ep.dai["file"])
    audio = store.path(ep.audio[ep.dai["stitched"]])
    overrides = json.loads((HERE / "sources" / "constructed-overrides.json").read_text()).get(eid, [])
    ads = constructed_ads(dai, transcriber, audio, ep.language, overrides)
    transcript_info = None
    method = "construction"
    if spec_path is not None:
        spec = json.loads(spec_path.read_text())
        official = load_transcript(store.path(ep.transcripts["official"]))
        ads += text_ads(spec, official, dai)
        transcript_info = {"name": "official", "file": ep.transcripts["official"].file,
                           "sha256": ep.transcripts["official"].sha256, "aligned_to": "clean",
                           "mapped_to_stitched_via": ep.dai["file"]}
        method = "mixed"
    fx = ep.audio[ep.dai["stitched"]]
    label = new_label(
        episode={"id": eid, "podcast": ep.podcast, "title": ep.title, "guid": ep.guid, "feed_url": ep.feed_url,
                 "enclosure_url": ep.enclosure_url, "audio_file": fx.file, "sha256": fx.sha256,
                 "duration_seconds": fx.duration, "variant": ep.dai["stitched"], "user_agent": fx.user_agent},
        provenance={"method": method, "drafted_by": DRAFTED_BY,
                    "notes": ("Inserted regions are exact splice points from podcleaner.eval.dai (clean vs podcatcher "
                              "download). " + ("Host-read segments were located by reading the publisher transcript and "
                              "mapped into this file's timeline; a human must confirm them by listening." if spec_path
                              else "The publisher transcript of the clean master contains no advertising, so these "
                              "regions are expected to be the complete set; a human pass confirms that."))},
        ads=ads, transcript=transcript_info,
        label_convention="cue_aligned" if spec_path else "break_including_pause",
    )
    return label


def build_hot(eid: str, episodes, store):
    ep = episodes[eid]
    src = json.loads((HERE / "sources" / "hot-oh-canada.whisper-transcribe.json").read_text())
    non_speech = {(ns["start"], ns["end"]) for ns in src.get("non_speech", [])}
    whisper = load_transcript(store.path(ep.transcripts["whisper-small"]))
    spoken = [c for c in whisper.cues if c.text.strip() and not c.text.strip().startswith("[")]
    ads = []
    for s in src["segments"]:
        start, end = s["start"], s["end"]
        head = tail = None
        # split music stings out of the ad so 'cut the sting or not' stays a don't-care
        for ns_start, ns_end in sorted(non_speech):
            if abs(ns_start - start) < 1e-6 and ns_end < end:
                head = (ns_start, ns_end)
            if abs(ns_end - end) < 1e-6 and ns_start > start:
                tail = (ns_start, ns_end)
        core_start = head[1] if head else start
        core_end = tail[0] if tail else end
        if head:
            ads.append({"start": head[0], "end": head[1], "category": "other", "ambiguous": True, "source": "text",
                        "verified": False, "inserted": False, "first_line": "[MUSIC]", "last_line": "[MUSIC]",
                        "note": "music sting before " + s["note"]})
        first_spoken = next((c.text for c in spoken if c.start >= core_start - 1e-6), s["first_line"])
        last_spoken = next((c.text for c in reversed(spoken) if c.end <= core_end + 1e-6), s["last_line"])
        ads.append({
            "start": round(core_start, 3), "end": round(core_end, 3), "category": s["category"],
            "ambiguous": bool(s.get("ambiguous", False)), "source": "text", "verified": False, "inserted": False,
            "start_cue": s["start_cue"], "end_cue": s["end_cue"],
            "first_line": first_spoken if head else s["first_line"],
            "last_line": last_spoken if tail else s["last_line"], "note": s["note"],
        })
        if tail:
            ads.append({"start": tail[0], "end": tail[1], "category": "other", "ambiguous": True, "source": "text",
                        "verified": False, "inserted": False, "first_line": "[MUSIC]", "last_line": "[MUSIC]",
                        "note": "music sting after " + s["note"]})
    extra_path = HERE / "sources" / "hot-oh-canada.extra-ads.json"
    if extra_path.exists():
        by_index = {c.index: c for c in whisper.cues}
        for seg in json.loads(extra_path.read_text())["segments"]:
            first, last = by_index[seg["start_cue"]], by_index[seg["end_cue"]]
            ads.append({
                "start": round(first.start, 3), "end": round(last.end, 3), "category": seg["category"],
                "ambiguous": bool(seg.get("ambiguous", False)), "source": "text", "verified": False, "inserted": False,
                "start_cue": seg["start_cue"], "end_cue": seg["end_cue"],
                "first_line": first.text, "last_line": last.text, "note": seg.get("note"),
            })
    fx = ep.audio["podcatcher"]
    tr = ep.transcripts["whisper-small"]
    return new_label(
        episode={"id": eid, "podcast": ep.podcast, "title": ep.title, "guid": ep.guid, "feed_url": ep.feed_url,
                 "enclosure_url": ep.enclosure_url, "audio_file": fx.file, "sha256": fx.sha256,
                 "duration_seconds": fx.duration, "variant": "podcatcher", "user_agent": None},
        provenance={"method": "text_annotation",
                    "drafted_by": "repo owner, 2026-08-31, by reading all 1286 whisper cues (whisper-transcribe/benchmark/ground-truth/oh-canada.json); converted 2026-09-06",
                    "notes": "Cue-aligned to the whisper transcript. Two segments the annotator flagged ambiguous stay don't-care; "
                             "music stings at ad edges were split out as don't-care so recall is measured on speech."},
        ads=ads,
        transcript={"name": "whisper-small", "file": tr.file, "sha256": tr.sha256, "aligned_to": "podcatcher"},
        label_convention="cue_aligned",
        notes="Dynamically inserted ad load; this file's ads differ from any other download of the episode.",
    )


def main() -> int:
    episodes = load_manifest(HERE.parent / "manifest.json")
    store = FixtureStore(allow_download=False)
    transcriber = WhisperCppTranscriber(cache_dir=ROOT / "var" / "cache",
                                        runner=lambda *_: (_ for _ in ()).throw(TranscriptionError("cache only")))
    outputs = {
        "ldn491": build_dai_episode("ldn491", episodes, store, transcriber, HERE / "sources" / "ldn491.text-ads.json"),
        "ldn490": build_dai_episode("ldn490", episodes, store, transcriber, None),
        "solved-life-path": build_dai_episode("solved-life-path", episodes, store, transcriber,
                                              HERE / "sources" / "solved-life-path.text-ads.json"),
        "hot-oh-canada": build_hot("hot-oh-canada", episodes, store),
    }
    for eid, label in outputs.items():
        path = HERE / f"{eid}.podcatcher.label.json"
        existing = json.loads(path.read_text()) if path.exists() else None
        if existing:
            # never discard a human's verification work on rebuild
            label["labeler"] = existing.get("labeler")
            label["status"] = existing.get("status", "in_progress")
            verified = {(a["start"], a["end"]): a.get("verified") for a in existing.get("ads", [])}
            for a in label["ads"]:
                if verified.get((a["start"], a["end"])):
                    a["verified"] = True
        save_label(path, label)
        (HERE / f"{eid}.VERIFY.md").write_text(checklist(label), encoding="utf-8")
        n_text = sum(1 for a in label["ads"] if a["source"] == "text")
        print(f"{path.name}: {len(label['ads'])} segments ({n_text} text-derived, "
              f"{sum(1 for a in label['ads'] if a['source'] == 'construction')} constructed), status={label['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
