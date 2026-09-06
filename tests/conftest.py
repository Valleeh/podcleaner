"""Test-suite gating.

The default run (``pytest``) is offline and fast: only ``tests/unit``.  Anything that
touches real audio, Docker, the network or a paid API is marked ``integration`` and is
skipped unless asked for:

    pytest --integration                  # whisper windows + ad detection (needs fixtures, docker, API key)
    pytest --integration --download       # fetch missing fixtures first
    pytest --integration --full           # also whole-episode work (hours)

Within an integration run, tests that cannot run on this host (no Docker image, no API
key, missing or mismatching fixture) *skip with the reason*, never pass vacuously.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("podcleaner")
    group.addoption("--integration", action="store_true", default=False,
                    help="run tests marked 'integration' (real audio, Docker, LLM API)")
    group.addoption("--full", action="store_true", default=False,
                    help="also run tests marked 'full' (whole-episode transcription; hours)")
    group.addoption("--download", action="store_true", default=False,
                    help="download missing fixtures listed in tests/integration/manifest.json")
    group.addoption("--record-baselines", action="store_true", default=False,
                    help="write measured metrics to tests/integration/baselines.json")
    group.addoption("--provisional-labels", action="store_true", default=False,
                    help="score against label files that no human has finished (measurement mode)")
    group.addoption("--whisper-model", default=None,
                    help="GGML model file to use instead of the default small-q5_1")
    group.addoption("--llm-fresh", action="store_true", default=False,
                    help="bypass the cached LLM replies under var/cache/llm (re-bills the API)")
    group.addoption("--llm-model", default=None,
                    help="OpenRouter model id for the ad-detection tests (default: PODCLEANER_LLM_MODEL or the code default)")


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    run_integration = config.getoption("--integration")
    run_full = config.getoption("--full")
    skip_integration = pytest.mark.skip(reason="integration test: pass --integration to run")
    skip_full = pytest.mark.skip(reason="whole-episode test: pass --full to run (hours)")
    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)
        elif "full" in item.keywords and not run_full:
            item.add_marker(skip_full)
