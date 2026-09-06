"""Fixtures for the integration suite: pinned episodes, whisper, the LLM, reports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from podcleaner.detect.llm import AdClassifier, LLMConfig, LLMError, OpenAICompatibleClient
from podcleaner.detect.transcribe import WhisperConfig, WhisperCppTranscriber
from podcleaner.eval.fixtures import FixtureStore, load_manifest

from .support import CACHE_DIR, Baselines, CachedCompletion, Report, build_classifier, config_for_model, load_catalogue


@pytest.fixture(scope="session")
def manifest():
    return load_manifest()


@pytest.fixture(scope="session")
def store(request):
    return FixtureStore(allow_download=request.config.getoption("--download"))


@pytest.fixture(scope="session")
def baselines(request):
    b = Baselines.load(record=request.config.getoption("--record-baselines"))
    yield b
    b.save()


@pytest.fixture(scope="session")
def report(request):
    r = Report()
    request.config._podcleaner_report = r  # picked up by pytest_terminal_summary
    yield r
    path = r.save()
    if path:
        request.config._podcleaner_report_path = path


@pytest.fixture(scope="session")
def transcriber(request):
    model = request.config.getoption("--whisper-model")
    cfg = WhisperConfig(model_path=Path(model)) if model else WhisperConfig()
    t = WhisperCppTranscriber(cfg, cache_dir=CACHE_DIR)
    ok, reason = t.availability()
    if not ok:
        pytest.skip(f"whisper.cpp not runnable here: {reason}")
    return t


@pytest.fixture(scope="session")
def catalogue():
    return load_catalogue()


@pytest.fixture(scope="session")
def llm_spec(request):
    from podcleaner.detect.cascade import DEFAULT_SPEC

    return request.config.getoption("--llm-model") or os.environ.get("PODCLEANER_LLM_SPEC") or DEFAULT_SPEC


@pytest.fixture(scope="session")
def llm_config(request, catalogue, llm_spec):
    _clf, cfg, _cost = build_classifier(llm_spec, catalogue, fresh=request.config.getoption("--llm-fresh"))
    try:
        cfg.resolved_api_key()
    except LLMError as exc:
        pytest.skip(f"no LLM backend: {exc}")
    return cfg


@pytest.fixture
def classifier(request, catalogue, llm_spec, llm_config):
    """A fresh classifier per test (call counters start at zero); replies are cached."""
    clf, _cfg, cost_fn = build_classifier(llm_spec, catalogue, fresh=request.config.getoption("--llm-fresh"))
    clf.cost_fn = cost_fn  # used by the tests to price the run
    clf.spec = llm_spec or _cfg.model
    return clf


@pytest.fixture(scope="session")
def provisional_ok(request):
    return bool(request.config.getoption("--provisional-labels"))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    report = getattr(config, "_podcleaner_report", None)
    if report and report.entries:
        terminalreporter.section("podcleaner integration metrics")
        for line in report.summary_lines():
            terminalreporter.write_line(line)
        path = getattr(config, "_podcleaner_report_path", None)
        if path:
            terminalreporter.write_line(f"report written to {path}")
