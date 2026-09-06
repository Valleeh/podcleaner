"""podcleaner.eval.labels: validation, gold extraction, provenance rules."""

from __future__ import annotations

import copy

import pytest

from podcleaner.eval.labels import LabelError, checklist, gold_ads, load_label, new_label, save_label

EPISODE = {"id": "ep", "audio_file": "ep.mp3", "sha256": "a" * 64, "duration_seconds": 1000.0}


def _label(**kw):
    ads = kw.pop("ads", [
        {"start": 10.0, "end": 40.0, "category": "sponsor_read", "source": "construction", "inserted": True},
        {"start": 100.0, "end": 130.0, "category": "sponsor_read", "source": "text", "first_line": "Buy this.", "last_line": "Use code X."},
        {"start": 130.0, "end": 133.0, "category": "other", "source": "text", "ambiguous": True},
    ])
    return new_label(episode=EPISODE, provenance={"method": "mixed"}, ads=ads, **kw)


def test_new_label_starts_in_progress_and_marks_construction_verified():
    lab = _label()
    assert lab["status"] == "in_progress"
    assert [a["verified"] for a in lab["ads"]] == [True, False, False]
    assert lab["provenance"]["all_segments_verified"] is False


def test_gold_refuses_unfinished_labels_unless_provisional():
    lab = _label()
    with pytest.raises(LabelError):
        gold_ads(lab)
    gold = gold_ads(lab, allow_provisional=True)
    assert [(g.start, g.end, g.ambiguous) for g in gold] == [(10, 40, False), (100, 130, False), (130, 133, True)]


def test_gold_refuses_wrong_audio_hash():
    lab = _label()
    with pytest.raises(LabelError):
        gold_ads(lab, allow_provisional=True, audio_sha256="b" * 64)
    assert gold_ads(lab, allow_provisional=True, audio_sha256="a" * 64)


def test_complete_requires_labeler_and_verified_text_segments():
    lab = _label()
    lab["status"] = "complete"
    with pytest.raises(LabelError, match="labeler"):
        save_label.__wrapped__(lab) if hasattr(save_label, "__wrapped__") else __import__("podcleaner.eval.labels", fromlist=["validate_label"]).validate_label(lab)
    lab["labeler"] = "someone"
    with pytest.raises(LabelError, match="not verified"):
        __import__("podcleaner.eval.labels", fromlist=["validate_label"]).validate_label(lab)
    for a in lab["ads"]:
        a["verified"] = True
    assert gold_ads(lab)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["ads"].append({"start": 20.0, "end": 30.0, "category": "sponsor_read", "source": "text"}),  # overlap
        lambda d: d["ads"].append({"start": 990.0, "end": 1200.0, "category": "sponsor_read", "source": "text"}),  # past end
        lambda d: d["ads"][0].update(category="advert"),
        lambda d: d["ads"][0].update(source="guessed"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(label_convention="whatever"),
        lambda d: d.__setitem__("extra", 1),
        lambda d: d["episode"].__setitem__("sha256", "short"),
    ],
)
def test_validation_rejects_structural_problems(mutate):
    from podcleaner.eval.labels import validate_label

    lab = copy.deepcopy(_label())
    mutate(lab)
    with pytest.raises(LabelError):
        validate_label(lab)


def test_roundtrip_and_checklist(tmp_path):
    lab = _label()
    path = tmp_path / "ep.label.json"
    save_label(path, lab)
    assert load_label(path) == lab
    text = checklist(lab)
    assert "Buy this." in text and "construction" in text and "don't-care" in text
