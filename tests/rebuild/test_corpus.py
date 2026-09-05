"""Step 1 tests for `podcleaner.eval.corpus` and `podcleaner.eval.label_cli`.

Covers verification-contract criteria S1.4 (constructed corpus + **ffprobe** oracle) and
S1.6 (`corpus/real/` ships empty; schema + human labelling CLI only).

Everything is synthesised locally by ffmpeg's `lavfi`. Nothing is downloaded, so the
suite passes offline.

The oracle discipline that matters here: durations and offsets are checked with
`ffprobe`/`ffmpeg`, which know nothing about `corpus.py`'s arithmetic. Checking our cut
math with our own cut code would be circular and would prove nothing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from podcleaner.eval import corpus as corpus_mod
from podcleaner.eval import label_cli
from podcleaner.eval.corpus import (
    AD_CREATIVES,
    FRAME_SECONDS,
    SAMPLE_RATE,
    CorpusError,
    ffprobe_duration_seconds,
    ffprobe_sample_count,
    generate_corpus,
    gold_intervals,
    load_manifest,
)
from podcleaner.eval.label_cli import LabelError, build_stub, validate_label
from podcleaner.eval.scoring import IntervalError, normalize_intervals, score

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CORPUS = REPO_ROOT / "corpus" / "real"

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required to build and to independently verify the corpus",
)

N_EPISODES = 6
N_AD_FREE = 2
SEED = 20240501


@pytest.fixture(scope="module")
def built_corpus(tmp_path_factory):
    """Build the corpus once for the whole module."""
    out = tmp_path_factory.mktemp("synthetic")
    manifest = generate_corpus(
        out, seed=SEED, n_episodes=N_EPISODES, n_ad_free_episodes=N_AD_FREE
    )
    return out, manifest


# --------------------------------------------------------------------------------------
# independent oracles -- deliberately implemented with external tools, not with corpus.py
# --------------------------------------------------------------------------------------


def _raw_pcm_md5(path: Path, start: float | None = None, duration: float | None = None):
    """Decode (a slice of) a file to raw s16le with ffmpeg and hash the samples.

    Used to verify *offsets*, not just totals: the bytes at the manifest's ad interval
    must be the creative's bytes.
    """
    cmd = [shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path)]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    proc = subprocess.run(cmd, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    return hashlib.sha256(proc.stdout).hexdigest(), len(proc.stdout)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ======================================================================================
# S1.4 -- constructed ground truth, verified with ffprobe
# ======================================================================================


def test_s1_4_manifest_is_written_and_well_formed(built_corpus):
    out, manifest = built_corpus
    on_disk = load_manifest(out)
    assert on_disk == manifest
    assert manifest["schema_version"] == 1
    assert manifest["seed"] == SEED
    assert manifest["sample_rate"] == SAMPLE_RATE
    assert len(manifest["episodes"]) == N_EPISODES
    assert len(manifest["creatives"]) == len(AD_CREATIVES)


def test_s1_4_every_episode_file_exists(built_corpus):
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        assert (out / episode["path"]).is_file()


def test_s1_4_ffprobe_confirms_episode_duration_equals_sum_of_parts(built_corpus):
    """S1.4, the headline: ffprobe's duration == the manifest's, within one frame.

    ffprobe is the independent oracle. The manifest duration is the sum of the part
    durations we spliced; an off-by-one anywhere in that sum (mutation (d)) makes the two
    disagree.
    """
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        path = out / episode["path"]
        probed = ffprobe_duration_seconds(path)
        summed = sum(part["duration_seconds"] for part in episode["parts"])

        assert episode["duration_seconds"] == pytest.approx(summed, abs=1e-9), episode["id"]
        assert abs(probed - episode["duration_seconds"]) <= FRAME_SECONDS, (
            f"{episode['id']}: ffprobe says {probed}s, manifest says "
            f"{episode['duration_seconds']}s"
        )


def test_s1_4_ffprobe_sample_count_is_exact(built_corpus):
    """Stricter than the one-frame tolerance: PCM sample counts must match exactly.

    This is what makes a genuine one-sample off-by-one impossible to hide inside the
    one-frame tolerance above.
    """
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        probed = ffprobe_sample_count(out / episode["path"])
        summed = sum(part["duration_samples"] for part in episode["parts"])
        assert probed == episode["duration_samples"], episode["id"]
        assert probed == summed, episode["id"]


def test_s1_4_ffprobe_confirms_each_creative_duration(built_corpus):
    out, manifest = built_corpus
    for creative in manifest["creatives"]:
        path = out / creative["path"]
        assert abs(
            ffprobe_duration_seconds(path) - creative["duration_seconds"]
        ) <= FRAME_SECONDS
        assert ffprobe_sample_count(path) == creative["duration_samples"]


def test_s1_4_the_audio_at_each_manifest_ad_interval_is_that_creative(built_corpus):
    """The strongest ground-truth check: the *offsets* are right, not just the totals.

    Extract exactly [start, end) from the episode with ffmpeg and compare the decoded
    samples with the standalone creative. A one-sample error in the cumulative offsets
    changes this hash.
    """
    out, manifest = built_corpus
    by_id = {c["id"]: c for c in manifest["creatives"]}
    checked = 0
    for episode in manifest["episodes"]:
        for ad in episode["ads"]:
            creative = by_id[ad["creative_id"]]
            expected_hash, expected_len = _raw_pcm_md5(out / creative["path"])
            actual_hash, actual_len = _raw_pcm_md5(
                out / episode["path"],
                start=ad["start"],
                duration=ad["end"] - ad["start"],
            )
            assert actual_len == expected_len, (episode["id"], ad)
            assert actual_hash == expected_hash, (
                f"{episode['id']}: audio at {ad['start']}..{ad['end']} is not "
                f"{ad['creative_id']}"
            )
            checked += 1
    assert checked > 0, "corpus produced no ad insertions to verify"


def test_s1_4_the_audio_just_before_an_ad_is_not_the_ad(built_corpus):
    """Negative control for the offset check above: shifting the window must break it.

    Without this, an extractor that ignored -ss entirely could pass the previous test.
    """
    out, manifest = built_corpus
    by_id = {c["id"]: c for c in manifest["creatives"]}
    checked = 0
    for episode in manifest["episodes"]:
        for ad in episode["ads"]:
            if ad["start"] < 1.0:
                continue
            creative = by_id[ad["creative_id"]]
            expected_hash, _ = _raw_pcm_md5(out / creative["path"])
            shifted_hash, _ = _raw_pcm_md5(
                out / episode["path"],
                start=ad["start"] - 1.0,
                duration=ad["end"] - ad["start"],
            )
            assert shifted_hash != expected_hash, (
                f"{episode['id']}: a window one second early still matched "
                f"{ad['creative_id']} -- the offset check is not actually checking offsets"
            )
            checked += 1
    assert checked > 0


def test_s1_4_ad_intervals_lie_inside_the_episode_and_do_not_overlap(built_corpus):
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        intervals = gold_intervals(episode)
        # would raise IntervalError if gold overlapped or was malformed; and because
        # every ad break is separated by content, normalisation must be a no-op
        normalized = normalize_intervals(intervals, allow_overlap=False, kind="gold")
        assert [list(iv) for iv in normalized] == intervals, episode["id"]
        for start, end in intervals:
            assert 0.0 <= start < end <= episode["duration_seconds"]


def test_s1_4_parts_tile_the_episode_without_gaps_or_overlaps(built_corpus):
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        cursor = 0.0
        for part in episode["parts"]:
            assert part["start"] == pytest.approx(cursor, abs=1e-9)
            cursor = part["end"]
        assert cursor == pytest.approx(episode["duration_seconds"], abs=1e-9)


def test_s1_4_manifest_gold_scores_zero_against_itself(built_corpus):
    """The corpus and the scorer agree: predicting the manifest exactly is perfect."""
    out, manifest = built_corpus
    for episode in manifest["episodes"]:
        gold = gold_intervals(episode)
        assert score(gold, gold).combined_score == 0.0


def test_s1_4_cutting_the_whole_episode_is_penalised_heavily(built_corpus):
    """Sanity coupling of corpus + scorer: a cut-everything detector scores badly."""
    out, manifest = built_corpus
    episode = next(ep for ep in manifest["episodes"] if ep["ads"])
    gold = gold_intervals(episode)
    result = score([[0.0, episode["duration_seconds"]]], gold)
    assert result.missed_ad_seconds == 0.0
    assert result.false_cut_seconds > 0.0
    assert result.combined_score > score([], gold).combined_score


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def test_same_seed_produces_an_identical_manifest(tmp_path):
    a = generate_corpus(tmp_path / "a", seed=99, n_episodes=3, n_ad_free_episodes=1)
    b = generate_corpus(tmp_path / "b", seed=99, n_episodes=3, n_ad_free_episodes=1)
    assert a == b


def test_same_seed_produces_byte_identical_audio(tmp_path):
    manifest = generate_corpus(tmp_path / "a", seed=99, n_episodes=3, n_ad_free_episodes=1)
    generate_corpus(tmp_path / "b", seed=99, n_episodes=3, n_ad_free_episodes=1)
    for episode in manifest["episodes"]:
        assert _file_digest(tmp_path / "a" / episode["path"]) == _file_digest(
            tmp_path / "b" / episode["path"]
        ), episode["id"]
    for creative in manifest["creatives"]:
        assert _file_digest(tmp_path / "a" / creative["path"]) == _file_digest(
            tmp_path / "b" / creative["path"]
        )


def test_different_seeds_produce_a_different_corpus(tmp_path):
    """Negative control for determinism: the seed must actually do something."""
    a = generate_corpus(tmp_path / "a", seed=1, n_episodes=4, n_ad_free_episodes=1)
    b = generate_corpus(tmp_path / "b", seed=2, n_episodes=4, n_ad_free_episodes=1)
    assert [ep["ads"] for ep in a["episodes"]] != [ep["ads"] for ep in b["episodes"]]


def test_regenerating_over_an_existing_directory_replaces_it(tmp_path):
    target = tmp_path / "c"
    generate_corpus(target, seed=5, n_episodes=2, n_ad_free_episodes=0)
    stale = target / "episodes" / "ep999.wav"
    stale.write_bytes(b"stale")
    generate_corpus(target, seed=5, n_episodes=2, n_ad_free_episodes=0)
    assert not stale.exists()
    assert not (target / ".work").exists()


# --------------------------------------------------------------------------------------
# negative controls in the corpus itself
# --------------------------------------------------------------------------------------


def test_corpus_contains_ad_free_episodes(built_corpus):
    """A detector that says 'yes' everywhere must have somewhere to be wrong."""
    _, manifest = built_corpus
    ad_free = [ep for ep in manifest["episodes"] if not ep["ads"]]
    assert len(ad_free) == N_AD_FREE
    for episode in ad_free:
        assert gold_intervals(episode) == []
        assert all(part["kind"] == "content" for part in episode["parts"])


def test_ad_free_episodes_punish_any_cut(built_corpus):
    out, manifest = built_corpus
    episode = next(ep for ep in manifest["episodes"] if not ep["ads"])
    perfect = score([], [])
    any_cut = score([[1.0, 2.0]], [])
    assert perfect.combined_score == 0.0
    assert any_cut.combined_score > 0.0


def test_corpus_contains_episodes_with_ads(built_corpus):
    _, manifest = built_corpus
    with_ads = [ep for ep in manifest["episodes"] if ep["ads"]]
    assert len(with_ads) == N_EPISODES - N_AD_FREE


def test_generate_corpus_rejects_nonsense_arguments(tmp_path):
    with pytest.raises(CorpusError):
        generate_corpus(tmp_path / "x", n_episodes=0)
    with pytest.raises(CorpusError):
        generate_corpus(tmp_path / "x", n_episodes=2, n_ad_free_episodes=3)
    with pytest.raises(CorpusError):
        generate_corpus(tmp_path / "x", n_episodes=2, max_ads_per_episode=0)


def test_ffprobe_helpers_reject_a_non_audio_file(tmp_path):
    junk = tmp_path / "not-audio.wav"
    junk.write_bytes(b"definitely not a wav")
    with pytest.raises(CorpusError):
        ffprobe_duration_seconds(junk)


def test_corpus_cli_writes_a_manifest(tmp_path, capsys):
    rc = corpus_mod._main(
        ["--out", str(tmp_path / "cli"), "--seed", "3", "--episodes", "2", "--ad-free", "1"]
    )
    assert rc == 0
    assert (tmp_path / "cli" / "manifest.json").is_file()
    assert "wrote 2 episodes" in capsys.readouterr().out


# ======================================================================================
# S1.6 -- corpus/real/ ships EMPTY, with a schema and a human labelling CLI
# ======================================================================================


def test_s1_6_real_corpus_directory_exists():
    assert REAL_CORPUS.is_dir()


def test_s1_6_real_corpus_contains_no_labels():
    """S1.6: any committed hand-label of a real episode is an automatic failure.

    Nobody has labelled a real episode, so there must be nothing here to find.
    """
    labels = sorted(REAL_CORPUS.glob(f"*{label_cli.LABEL_SUFFIX}"))
    assert labels == [], f"corpus/real/ must ship empty; found {labels}"


def test_s1_6_real_corpus_contains_only_documentation():
    """Only the schema and the README are committed -- no data of any kind."""
    present = sorted(p.name for p in REAL_CORPUS.iterdir() if p.name != ".gitkeep")
    assert present == ["README.md", "SCHEMA.json"], present


def test_s1_6_no_json_file_in_the_real_corpus_asserts_any_ad_interval():
    """Belt and braces: even a differently-named JSON must not carry ad intervals."""
    for path in REAL_CORPUS.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert not (isinstance(data, dict) and data.get("ads")), (
            f"{path} asserts ad labels for a real episode that nobody labelled"
        )


def test_s1_6_schema_file_matches_the_cli_schema():
    """The documented schema and the enforced schema must not drift apart."""
    on_disk = json.loads((REAL_CORPUS / "SCHEMA.json").read_text(encoding="utf-8"))
    assert on_disk == label_cli.LABEL_SCHEMA


def test_s1_6_readme_documents_the_schema_and_the_workflow():
    text = (REAL_CORPUS / "README.md").read_text(encoding="utf-8")
    for needle in ("SCHEMA.json", "label_cli", "in_progress", "complete", "labeler"):
        assert needle in text


def test_s1_6_synthetic_corpus_is_gitignored():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "corpus/synthetic/" in [line.strip() for line in ignore]


# --------------------------------------------------------------------------------------
# the labelling CLI: it must never invent a label
# --------------------------------------------------------------------------------------


def test_label_stub_starts_empty_and_in_progress(built_corpus, tmp_path):
    """`init` produces zero intervals. The tool contributes no opinions."""
    out, manifest = built_corpus
    audio = out / manifest["episodes"][0]["path"]
    stub = build_stub(audio, "a-human")
    assert stub["ads"] == []
    assert stub["status"] == "in_progress"
    assert stub["labeler"] == "a-human"
    assert stub["episode"]["sha256"] is not None
    assert stub["episode"]["duration_seconds"] == pytest.approx(
        manifest["episodes"][0]["duration_seconds"], abs=FRAME_SECONDS
    )
    validate_label(stub)


def test_label_stub_requires_a_named_human(built_corpus):
    out, manifest = built_corpus
    audio = out / manifest["episodes"][0]["path"]
    with pytest.raises(LabelError):
        build_stub(audio, "   ")


def test_label_stub_refuses_a_missing_file_without_an_explicit_duration(tmp_path):
    with pytest.raises(LabelError):
        build_stub(tmp_path / "nope.mp3", "a-human")


def test_label_cli_round_trip(built_corpus, tmp_path, capsys):
    """init -> add -> add -> finish -> validate, all through the CLI."""
    out, manifest = built_corpus
    audio = out / manifest["episodes"][0]["path"]
    label_path = tmp_path / "ep.label.json"

    assert (
        label_cli.main(
            [
                "init",
                "--episode",
                str(audio),
                "--labeler",
                "a-human",
                "--out",
                str(label_path),
            ]
        )
        == 0
    )
    assert json.loads(label_path.read_text())["ads"] == []

    assert (
        label_cli.main(
            ["add", "--label", str(label_path), "--start", "5", "--end", "9",
             "--kind", "host_read"]
        )
        == 0
    )
    # deliberately added out of order -- the CLI must sort
    assert (
        label_cli.main(
            ["add", "--label", str(label_path), "--start", "1", "--end", "3"]
        )
        == 0
    )
    assert label_cli.main(["finish", "--label", str(label_path)]) == 0

    data = json.loads(label_path.read_text())
    assert data["status"] == "complete"
    assert [(a["start"], a["end"]) for a in data["ads"]] == [(1.0, 3.0), (5.0, 9.0)]
    assert label_cli.main(["validate", str(label_path)]) == 0

    # and the result is directly usable as gold
    gold = [[a["start"], a["end"]] for a in data["ads"]]
    assert score(gold, gold).combined_score == 0.0


def test_label_cli_init_refuses_to_clobber(built_corpus, tmp_path):
    out, manifest = built_corpus
    audio = out / manifest["episodes"][0]["path"]
    label_path = tmp_path / "ep.label.json"
    args = ["init", "--episode", str(audio), "--labeler", "h", "--out", str(label_path)]
    assert label_cli.main(args) == 0
    assert label_cli.main(args) == 2
    assert label_cli.main(args + ["--force"]) == 0


def test_label_cli_add_refuses_an_overlapping_interval(built_corpus, tmp_path):
    out, manifest = built_corpus
    audio = out / manifest["episodes"][0]["path"]
    label_path = tmp_path / "ep.label.json"
    label_cli.main(
        ["init", "--episode", str(audio), "--labeler", "h", "--out", str(label_path)]
    )
    label_cli.main(["add", "--label", str(label_path), "--start", "5", "--end", "9"])
    before = label_path.read_text()
    assert (
        label_cli.main(
            ["add", "--label", str(label_path), "--start", "7", "--end", "12"]
        )
        == 2
    )
    assert label_path.read_text() == before, "a rejected add must not be written"


def test_label_cli_add_refuses_an_interval_past_the_end(built_corpus, tmp_path):
    out, manifest = built_corpus
    episode = manifest["episodes"][0]
    audio = out / episode["path"]
    label_path = tmp_path / "ep.label.json"
    label_cli.main(
        ["init", "--episode", str(audio), "--labeler", "h", "--out", str(label_path)]
    )
    past_end = episode["duration_seconds"] + 10.0
    assert (
        label_cli.main(
            [
                "add",
                "--label",
                str(label_path),
                "--start",
                str(episode["duration_seconds"] - 1),
                "--end",
                str(past_end),
            ]
        )
        == 2
    )


BAD_LABELS = [
    pytest.param({}, id="empty"),
    pytest.param({"schema_version": 1}, id="missing-everything"),
    pytest.param("not an object", id="not-an-object"),
]


@pytest.mark.parametrize("bad", BAD_LABELS)
def test_validate_label_rejects_structural_garbage(bad):
    with pytest.raises(LabelError):
        validate_label(bad)


def _good_label(**overrides):
    base = {
        "schema_version": 1,
        "episode": {"audio_path": "ep.mp3", "duration_seconds": 100.0},
        "labeler": "a-human",
        "labeled_at": "2024-01-01T00:00:00+00:00",
        "status": "complete",
        "ads": [{"start": 10.0, "end": 20.0}],
    }
    base.update(overrides)
    return base


def test_validate_label_accepts_a_good_document():
    assert validate_label(_good_label()) is not None


def test_validate_label_accepts_an_empty_complete_label():
    """'complete' with no ads is a real claim: a human heard no ads."""
    validate_label(_good_label(ads=[]))


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"schema_version": 2}, "wrong-version"),
        ({"labeler": ""}, "no-human-named"),
        ({"labeler": "   "}, "blank-human"),
        ({"status": "done"}, "unknown-status"),
        ({"ads": "10-20"}, "ads-not-a-list"),
        ({"ads": [{"start": 20.0, "end": 10.0}]}, "end-before-start"),
        ({"ads": [{"start": 0.0, "end": 10.0}, {"start": 5.0, "end": 15.0}]}, "overlap"),
        ({"ads": [{"start": 10.0, "end": 500.0}]}, "past-episode-end"),
        ({"ads": [{"start": 10.0}]}, "missing-end"),
        ({"ads": [{"start": 10.0, "end": 20.0, "kind": "made_up"}]}, "bad-kind"),
        ({"ads": [{"start": 10.0, "end": 20.0, "bogus": 1}]}, "unknown-ad-key"),
        ({"episode": {"audio_path": "x", "duration_seconds": 0}}, "zero-duration"),
        ({"episode": {"audio_path": "x"}}, "no-duration"),
        ({"bogus_top_level": True}, "unknown-top-level-key"),
    ],
)
def test_validate_label_negative_controls(overrides, reason):
    with pytest.raises(LabelError):
        validate_label(_good_label(**overrides))


def test_validate_label_holds_gold_to_the_scorer_standard():
    """NaN in a hand label must be refused by exactly the same rule the scorer uses."""
    with pytest.raises((LabelError, IntervalError)):
        validate_label(_good_label(ads=[{"start": float("nan"), "end": 10.0}]))


def test_label_cli_list_on_the_empty_real_corpus_says_so(capsys):
    assert label_cli.main(["list", "--dir", str(REAL_CORPUS)]) == 0
    assert "ships empty" in capsys.readouterr().out


def test_label_cli_schema_command_prints_valid_json(capsys):
    assert label_cli.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out) == label_cli.LABEL_SCHEMA


def test_label_cli_validate_reports_failure_for_a_bad_file(tmp_path, capsys):
    bad = tmp_path / "bad.label.json"
    bad.write_text("{not json", encoding="utf-8")
    assert label_cli.main(["validate", str(bad)]) == 1
