# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

No pytest/venv is currently provisioned in this checkout — set one up first:

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e . && pip install pytest pytest-cov
```

```bash
pytest tests/                                  # full suite
pytest tests/test_ad_detector.py -v            # one file
pytest tests/test_ad_detector.py::test_name    # one test
pytest --cov=podcleaner tests/                 # coverage (CI enforces >= 55%)
./run_tests_and_build.sh                       # pytest -xvs, then docker-compose build && up -d
docker-compose up -d                           # full stack: mosquitto, MinIO, all 5 services
```

`tests/test_system.py` is an end-to-end suite that expects an already-running stack at
`http://localhost:8080` (override via `tests/system_test_config.json` or `PODCLEANER_TEST_CONFIG`).
It hits real network podcast URLs and self-skips when the system isn't reachable.

Run a single service locally (needs an MQTT broker reachable at `message_broker.mqtt.host`):

```bash
python -m podcleaner service --service web|transcriber|ad-detector|audio-processor|downloader|all
python -m podcleaner process <podcast-url>     # one-shot CLI; publishes to the broker and waits
```

## Architecture

Five independent processes talk only over MQTT — there are no direct calls between services.
Everything routes through `Topics` constants in `podcleaner/services/message_broker.py`
(`podcast.<stage>.request` / `.complete` / `.failed`), and `Message.correlation_id` is the
request id that threads a single podcast through the whole pipeline.

**The web server is the orchestrator.** Individual services are dumb: each subscribes to its own
`*.request` topic, does its job, and publishes `*.complete`/`*.failed`. `WebServer._handle_*_complete`
in `services/web_server.py` is what chains stages together — it receives `download.complete` and
publishes `transcribe.request`, receives `transcribe.complete` and publishes `ad_detection.request`,
and so on. If you add a pipeline stage, the wiring goes in `WebServer._setup_subscriptions` and its
handlers, not in the services themselves.

Pipeline: downloader → transcriber (Whisper) → ad detector (LLM, chunked) → audio processor
(pydub cuts ad ranges). `models.py` holds the shared `Segment`/`Transcript` dataclasses that move
between stages as JSON.

`MessageBroker` is an ABC with `MQTTMessageBroker` (production, used by all entrypoints) and
`InMemoryMessageBroker` (tests). `ObjectStorage` in `services/object_storage.py` is the same shape:
a facade over `LocalStorageAdapter` and `S3StorageAdapter` (S3 adapter also serves MinIO).

Entrypoints: `__main__.py` parses the CLI and delegates `service` mode to `run_service.py`, which
constructs the broker plus the requested service objects and blocks on signals. `run_service.py` is
where per-service construction and shutdown ordering live.

### Idempotency / state

Every service keeps a `processed_files` set persisted as JSON in a hardcoded `debug_output/`
directory (`transcriber_processed_files.json`, `processed_files.json`, etc.) and skips work for
paths it has already handled. Stale entries there will silently make a service no-op — delete the
relevant JSON when re-testing a pipeline stage.

### HTTP API

`services/web_server.py` uses stdlib `BaseHTTPRequestHandler` (not FastAPI, despite it being in
requirements). GET only: `/process?url=`, `/rss?url=`, `/status?id=`, `/download/<file_id>`.

## Known inconsistencies

These are live bugs/mismatches in the tree — check against them before assuming a code path works:

- `run_service.py` calls `Transcriber(config=config.llm, ...)`, but `Transcriber.__init__` takes
  `(message_broker, model_name)` and has no `config` kwarg — the transcriber service raises on start.
- `__main__.py` process mode calls `PodcastDownloader(config=config.audio, ...)`, but the downloader
  expects the full `Config` (it reads `config.audio` and `config.object_storage`).
- `docker-compose.yml` entrypoints run `python -m podcleaner server <name> -c ...`; the CLI subcommand
  is `service --service <name>`. Compose also inlines its own `config.yaml` via heredoc, ignoring the
  repo's.
- Several `config.yaml` sections are never read by `config.py`: `transcriber.*`, `ad_detector.*`,
  `paths.*`, `web_server.base_url`. `ObjectStorageConfig.from_dict` reads `access_key`/`secret_key`/
  `region`/`endpoint_url` at the top level of `object_storage`, so the nested `s3:` and `local:`
  blocks in `config.yaml` (and in the README) have no effect.
- `tests/test_system.py` polls `/health`, which the request handler does not route (404).
- `LLMConfig.validate()` falls back to reading `OPENAI_API_KEY` from a gitignored `secrets.json`,
  but `Config.validate()` is never called by any entrypoint — the key comes from `config.yaml` or
  the `OPENAI_API_KEY` env var instead.

## Conventions

Logging is structlog via `podcleaner/logging.py`: events are snake_case names with keyword fields,
e.g. `logger.info("download_complete", url=url, path=path)` — not formatted sentences.

Config supports `${VAR:-default}` substitution, resolved in `load_config` before YAML parsing.
